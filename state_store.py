# ==============================================================
# state_store.py — Actions が持ち越す状態ファイルを BigQuery（private.state_files）に置く
# ==============================================================
# 【表】<project>.private.state_files（location asia-northeast1・path でクラスタ化）
#   1行＝1ファイルの1版の1断片。path・version_id・saved_at・sha256・size・deleted・part・parts・
#   content（ファイル全体を gzip したバイト列を STATE_STORE_CHUNK_BYTES ごとに分けたもの）・run_id・source。
#   削除は deleted=TRUE・content なしの行（削除の印）で表す。パスごとに saved_at（同時刻なら version_id）が
#   最も新しい版が「今の中身」。同時に走った workflow は、パスごとに最後に保存した方が勝つ。
#   公開しない資料（構成管理資料/・investment_dept/・.claude/skills/invest-*）も同じ表に置く（save_scoped）。
#   これらは STATE_STORE_PATHS に入れない。save は対象のパスの範囲にしか削除の印を付けないので、
#   Actions の save が「手元に無い」資料を削除と見なすことはない。
# 【費用】
#   書き込み … load job だけ（無料）。ストリーミング挿入は課金されるので使わない。
#   読み出し … tabledata.list（client.list_rows）だけ（無料）。クエリで読むとクエリ枠を使うため使わない。
#   掃除     … prune だけがクエリ（MERGE … DELETE）。dryRun で見積もり、STATE_STORE_PRUNE_MAX_BYTES を
#              超えたら実行しない。表全体の path・version_id・saved_at 列だけを読む（数MB〜数十MB）。
# 【サブコマンド】
#   prepare  checkout 直後に呼ぶ。bq なら restore（＋基準の記録）、dual なら基準の記録だけ、git なら何もしない
#   save     変わったファイルだけを load job で追記する（git モードでは何もしない）
#   restore  パスごとに最新の版をファイルに書き戻す
#   baseline 今のファイルの sha を基準として記録する（save が「この実行で変えたか」を判断する材料）
#   prune    パスごとに新しい版を STATE_STORE_KEEP_VERSIONS だけ残して古い版を消す
# 【出力】件数・サイズ・所要時間だけを出す（パス名には銘柄名が入るものがあるため出さない）。
# 【認証】BQ_CREDENTIALS（JSON 文字列・Actions）→ BQ_CREDENTIALS_PATH → GOOGLE_APPLICATION_CREDENTIALS（Mac）。
# ==============================================================
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BASELINE_NAME = ".state_store_baseline.json"
MODES = ("git", "dual", "bq")
# 変わったパスがこれ以下なら、行番号を指定して1行ずつ取り出す（表全体を読まずに済む）
INDEXED_FETCH_MAX = 200
# 全件読みの区間の行数と並列数（tabledata.list は無料。ページの往復待ちを重ねて隠す）
FULL_FETCH_SPAN = 250
FULL_FETCH_WORKERS = 8
# 要る行だけを読むときの区間のまとめ方。要る行は表の中に散らばっているので、間を広くまとめると要らない行まで読む
# （64行でまとめると表の87%を読んだ）。間2行・16本並列で、1,153行（50MB）が手元で13秒（全件読みは82秒）
SPAN_GAP = 2
SPAN_WORKERS = 16

SCHEMA = [
    ("path", "STRING", "REQUIRED"),
    ("version_id", "STRING", "REQUIRED"),
    ("saved_at", "TIMESTAMP", "REQUIRED"),
    ("sha256", "STRING", "NULLABLE"),
    ("size", "INTEGER", "NULLABLE"),
    ("deleted", "BOOLEAN", "NULLABLE"),
    ("part", "INTEGER", "NULLABLE"),
    ("parts", "INTEGER", "NULLABLE"),
    ("content", "BYTES", "NULLABLE"),
    ("run_id", "STRING", "NULLABLE"),
    ("source", "STRING", "NULLABLE"),
]
META_FIELDS = ("path", "version_id", "saved_at", "sha256", "size", "deleted", "part", "parts")


# ── 設定 ─────────────────────────────────────────────────────

def load_config():
    spec = importlib.util.spec_from_file_location("config", str(REPO_ROOT / "config.py"))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg


def resolve_mode(cfg=None) -> str:
    """環境変数 STATE_STORE_MODE → config の順。知らない値は git（今のまま）に倒す。"""
    m = (os.environ.get("STATE_STORE_MODE") or getattr(cfg, "STATE_STORE_MODE", "") or "git").strip().lower()
    return m if m in MODES else "git"


# ── 純関数（走査・差分・分割・版の選択） ─────────────────────

def row_spans(indices, gap: int | None = None, max_len: int | None = None) -> list[tuple[int, int]]:
    """昇順の行番号 → 読む区間 [(開始, 行数)]。間が gap 以下なら1つの区間にまとめ（往復の回数を減らす）、
    1区間は max_len 行まで（並列に読めるよう分ける）。"""
    max_len = int(max_len or FULL_FETCH_SPAN)
    gap = SPAN_GAP if gap is None else int(gap)
    spans: list[list[int]] = []
    for i in indices:
        if spans and i - (spans[-1][0] + spans[-1][1] - 1) <= gap and i - spans[-1][0] < max_len:
            spans[-1][1] = i - spans[-1][0] + 1
        else:
            spans.append([i, 1])
    return [(a, n) for a, n in spans]


def is_excluded(rel: str, excludes) -> bool:
    """rel（/ 区切りの相対パス）が除外に当たるか。'/' を含む指定は前方一致、含まない指定は名前の一致。"""
    parts = rel.split("/")
    for ex in excludes:
        if not ex:
            continue
        if ex.endswith("/") and "/" not in ex[:-1]:
            if ex[:-1] in parts[:-1]:
                return True
        elif "/" in ex:
            if rel.startswith(ex):
                return True
        elif ex in parts:
            return True
    return rel == BASELINE_NAME or rel.endswith("/" + BASELINE_NAME)


def in_scope(rel: str, paths) -> bool:
    """rel が対象のパス（フォルダは末尾 /）のどれかに入るか。"""
    for p in paths:
        if (p.endswith("/") and rel.startswith(p)) or rel == p:
            return True
    return False


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def scan(root: Path, paths, excludes) -> dict:
    """root からの相対パス → {sha256, size}。シンボリックリンクは追わない。"""
    root = Path(root)
    out = {}
    for p in paths:
        base = root / p.rstrip("/")
        if p.endswith("/"):
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames.sort()
                for name in sorted(filenames):
                    fp = Path(dirpath) / name
                    if fp.is_symlink() or not fp.is_file():
                        continue
                    rel = fp.relative_to(root).as_posix()
                    if is_excluded(rel, excludes):
                        continue
                    out[rel] = {"sha256": sha256_file(fp), "size": fp.stat().st_size}
        elif base.is_file() and not base.is_symlink():
            rel = base.relative_to(root).as_posix()
            if not is_excluded(rel, excludes):
                out[rel] = {"sha256": sha256_file(base), "size": base.stat().st_size}
    return out


def split_chunks(data: bytes, size: int) -> list:
    """bytes を size ごとに分ける。空でも1断片（空ファイルの版を表すため）。"""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if not data:
        return [b""]
    return [data[i:i + size] for i in range(0, len(data), size)]


def _ts(v) -> float:
    """saved_at（datetime・ISO 文字列・数値）→ UNIX 秒。比較用。"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp()
    s = str(v).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def version_key(row: dict) -> tuple:
    return (_ts(row.get("saved_at")), str(row.get("version_id") or ""))


def latest_versions(rows) -> dict:
    """メタ行の並び → path → 最新版 {version_id, saved_at, sha256, size, deleted, parts}。"""
    best: dict = {}
    for r in rows:
        p = r.get("path")
        if not p:
            continue
        k = version_key(r)
        cur = best.get(p)
        if cur is None or k > cur["_key"]:
            best[p] = {"_key": k, "version_id": r.get("version_id"), "saved_at": _ts(r.get("saved_at")),
                       "sha256": r.get("sha256"), "size": r.get("size"),
                       "deleted": bool(r.get("deleted")), "parts": r.get("parts") or 1}
    return best


def plan_changes(local: dict, latest: dict, baseline: dict | None, baseline_at: float | None,
                 scope_paths=None, excludes=()) -> dict:
    """保存すべきもの（upload・delete）を決める。

    local    : scan() の結果（今のファイル）
    latest   : latest_versions()（BQ の今の中身）
    baseline : 実行の最初（checkout／restore の直後）の path → sha。無ければ None
    同時に走った別の実行が、この実行の開始後に保存したパスを、この実行が触っていない
    （今の sha が基準と同じ）なら上書きしない。git の「この実行で変えたファイルだけ commit」と同じ考え。
    BQ の版が基準より古いなら、手元（git から来た新しい中身など）を保存して揃える。"""
    base = baseline or {}
    upload, delete, skipped_newer = [], [], 0
    paths = set(local)
    for p, v in latest.items():
        if v.get("deleted"):
            continue
        if scope_paths is not None and not in_scope(p, scope_paths):
            continue
        if is_excluded(p, excludes):
            continue
        paths.add(p)
    for p in sorted(paths):
        L = (local.get(p) or {}).get("sha256")
        cur = latest.get(p)
        B = None if (cur is None or cur.get("deleted")) else cur.get("sha256")
        if L == B:
            continue
        if (baseline_at is not None and cur is not None and cur["saved_at"] > baseline_at
                and L == base.get(p)):
            skipped_newer += 1
            continue
        if L is None:
            delete.append(p)
        else:
            upload.append(p)
    return {"upload": upload, "delete": delete, "skipped_newer": skipped_newer}


def make_version_id(source: str, run_id: str, entries, saved_at: str) -> str:
    """内容から版の ID（＝load job の ID の材料）を決める。同じ実行の取り直しは同じ ID になる。
    run_id が無い手元の実行は、同じ中身を時間をおいて保存し直すことがあるので保存時刻も混ぜる。"""
    body = json.dumps({"s": source, "r": run_id, "e": sorted(entries),
                       "t": "" if run_id else saved_at}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(body.encode()).hexdigest()[:20]


def build_rows(rel: str, data: bytes | None, version_id: str, saved_at: str, run_id: str,
               source: str, chunk_bytes: int) -> list:
    """1ファイル → 表の行（content は base64 文字列。NDJSON の BYTES 列の書式）。data=None は削除の印。"""
    common = {"path": rel, "version_id": version_id, "saved_at": saved_at,
              "run_id": run_id, "source": source}
    if data is None:
        return [{**common, "sha256": None, "size": 0, "deleted": True, "part": 0, "parts": 0, "content": None}]
    gz = gzip.compress(data, compresslevel=6, mtime=0)
    chunks = split_chunks(gz, chunk_bytes)
    sha = sha256_bytes(data)
    return [{**common, "sha256": sha, "size": len(data), "deleted": False, "part": i,
             "parts": len(chunks), "content": base64.b64encode(c).decode("ascii")}
            for i, c in enumerate(chunks)]


def assemble(parts: dict, n: int, sha: str | None) -> bytes | None:
    """断片 {part: bytes} → 元のバイト列。欠け・sha 不一致は None。"""
    if n <= 0 or any(i not in parts for i in range(n)):
        return None
    try:
        data = gzip.decompress(b"".join(parts[i] or b"" for i in range(n)))
    except (OSError, EOFError):
        return None
    if sha and sha256_bytes(data) != sha:
        return None
    return data


def dest_for(rel: str, root: Path, strip: str = "") -> Path:
    if strip and rel.startswith(strip):
        rel = rel[len(strip):]
    return Path(root) / rel


def _content_bytes(v) -> bytes:
    if v is None:
        return b""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return base64.b64decode(v)


# ── 基準（baseline） ─────────────────────────────────────────

def write_baseline(path: Path, files: dict, created_at: float | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"created_at": created_at if created_at is not None else time.time(),
                                "files": {k: v["sha256"] for k, v in files.items()}},
                               ensure_ascii=False), encoding="utf-8")


def read_baseline(path: Path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d.get("files") or {}, float(d["created_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


# ── BigQuery とのやりとり（テストでは偽物に差し替える） ────────

class BQBackend:
    """load job と tabledata.list だけで読み書きする。"""

    def __init__(self, cfg, client=None):
        from google.cloud import bigquery
        self.bq = bigquery
        self.cfg = cfg
        self.client = client or make_client(cfg)
        self.location = getattr(cfg, "STATE_STORE_LOCATION", "asia-northeast1")
        self.dataset_ref = f"{self.client.project}.{getattr(cfg, 'STATE_STORE_DATASET', 'private')}"
        self.table_ref = f"{self.dataset_ref}.{getattr(cfg, 'STATE_STORE_TABLE', 'state_files')}"
        self._schema = [bigquery.SchemaField(n, t, mode=m) for n, t, m in SCHEMA]

    def ensure(self) -> None:
        """データセットと表が無ければ作る（メタデータ API・課金なし）。"""
        ds = self.bq.Dataset(self.dataset_ref)
        ds.location = self.location
        ds.description = "Actions が持ち越す状態ファイル（state_store.py）"
        self.client.create_dataset(ds, exists_ok=True)
        t = self.bq.Table(self.table_ref, schema=self._schema)
        t.clustering_fields = ["path"]
        self.client.create_table(t, exists_ok=True)

    def _fields(self, names):
        return [f for f in self._schema if f.name in names]

    def iter_meta(self):
        """(行番号, メタ行) を表の並び順に返す（content は読まない）。"""
        rows = self.client.list_rows(self.table_ref, selected_fields=self._fields(META_FIELDS),
                                     page_size=20000)
        for i, r in enumerate(rows):
            yield i, dict(r.items())

    def iter_spans(self, spans):
        """[(開始の行番号, 行数)] の区間を並列に読み、(行番号, 行) を区間の順に返す。"""
        from concurrent.futures import ThreadPoolExecutor

        def _read(span):
            start, n = span
            rows = self.client.list_rows(self.table_ref, selected_fields=self._schema, start_index=start,
                                         max_results=n, page_size=50)
            return start, [dict(r.items()) for r in rows]

        with ThreadPoolExecutor(max_workers=int(SPAN_WORKERS)) as ex:
            for start, chunk in ex.map(_read, spans):
                for k, r in enumerate(chunk):
                    yield start + k, r

    def iter_full(self):
        """全行を (行番号, 行) で返す。1ページ50行を順に読むとページごとの往復待ちが積み重なる
        （7,000行で約3〜7分）ので、行番号の区間に分けて並列に読み、表の並び順で返す。"""
        from concurrent.futures import ThreadPoolExecutor
        total = int(self.table_info().get("num_rows") or 0)
        span = int(FULL_FETCH_SPAN)

        def _read(start):
            rows = self.client.list_rows(self.table_ref, selected_fields=self._schema, start_index=start,
                                         max_results=span, page_size=50)
            return [dict(r.items()) for r in rows]

        # 行数は読み始めの時点。restore が欲しい版はその前に読んだメタに載っているので、後から足された行は要らない。
        # 掃除で並びが変わった場合は assemble の sha256 照合で欠けが分かる
        with ThreadPoolExecutor(max_workers=int(FULL_FETCH_WORKERS)) as ex:
            for n, chunk in enumerate(ex.map(_read, range(0, total, span))):
                for k, r in enumerate(chunk):
                    yield n * span + k, r

    def fetch_row(self, index: int):
        rows = list(self.client.list_rows(self.table_ref, selected_fields=self._schema,
                                          start_index=index, max_results=1))
        return dict(rows[0].items()) if rows else None

    def load_file(self, ndjson_path: Path, job_id: str) -> bool:
        from google.api_core.exceptions import Conflict
        jc = self.bq.LoadJobConfig(schema=self._schema,
                                   source_format=self.bq.SourceFormat.NEWLINE_DELIMITED_JSON,
                                   write_disposition=self.bq.WriteDisposition.WRITE_APPEND)
        for attempt in range(4):
            jid = job_id if attempt == 0 else f"{job_id}_r{attempt}"
            try:
                with open(ndjson_path, "rb") as fh:
                    self.client.load_table_from_file(fh, self.table_ref, job_id=jid, job_config=jc,
                                                     location=self.location).result()
                return True
            except Conflict:
                old = self.client.get_job(jid, location=self.location)
                if old.state == "DONE" and not old.error_result:
                    print("  ↩️ 同じ内容の load job は成功済み（書き込みなし）")
                    return True
            except Exception as e:
                print(f"  ❌ load job 失敗: {type(e).__name__}: {str(e)[:160]}")
                return False
        return False

    def query(self, sql: str, params: list, dry_run: bool, max_bytes: int):
        jc = self.bq.QueryJobConfig(query_parameters=params, dry_run=dry_run, use_query_cache=False)
        if not dry_run:
            jc.maximum_bytes_billed = int(max_bytes)
        job = self.client.query(sql, job_config=jc, location=self.location)
        if not dry_run:
            job.result()
        return job

    def table_info(self):
        t = self.client.get_table(self.table_ref)
        return {"num_rows": t.num_rows, "num_bytes": t.num_bytes}


def make_client(cfg):
    """BQ クライアント。鍵の中身は表示しない。"""
    from google.cloud import bigquery
    from google.oauth2 import service_account
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    project = (os.environ.get("BQ_PROJECT_ID") or getattr(cfg, "BQ_PROJECT_ID", "") or "").strip()
    js = os.environ.get("BQ_CREDENTIALS", "")
    path = (os.environ.get("BQ_CREDENTIALS_PATH") or getattr(cfg, "BQ_CREDENTIALS_PATH", "") or "").strip()
    if js:
        try:
            info = json.loads(js)
        except ValueError:
            raise SystemExit(
                f"❌ BQ_CREDENTIALS が JSON として読めない（長さ {len(js)} 文字・先頭が '{{' でない）。"
                "鍵ファイルのパスではなく、ファイルの中身そのものを入れる"
                "（gh secret set BQ_CREDENTIALS < 鍵.json）")
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        project = project or info.get("project_id", "")
    elif path:
        creds = service_account.Credentials.from_service_account_file(path, scopes=scopes)
        project = project or getattr(creds, "project_id", "") or ""
    else:
        import google.auth
        creds, default_project = google.auth.default(scopes=scopes)
        project = project or default_project or getattr(creds, "project_id", "") or ""
    if not project:
        raise SystemExit("❌ BQ のプロジェクトが決まらない（BQ_PROJECT_ID か鍵の project_id）")
    return bigquery.Client(project=project, credentials=creds)


# ── 本体 ─────────────────────────────────────────────────────

@dataclass
class SaveResult:
    scanned: int = 0
    uploaded: int = 0
    deleted: int = 0
    skipped_newer: int = 0
    bytes_raw: int = 0
    bytes_gz: int = 0
    jobs: int = 0
    ok: bool = True
    version_id: str = ""
    extra: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def save(backend, root: Path, paths, excludes, *, source: str, run_id: str = "",
         baseline_path: Path | None = None, chunk_bytes: int = 4 * 1024 ** 2,
         batch_bytes: int = 256 * 1024 ** 2, tmpdir: Path | None = None,
         delete_missing: bool = True) -> SaveResult:
    """paths（root からの相対）を走査し、BQ の最新と違うファイルだけを load job で追記する。
    delete_missing=False なら、手元に無いファイルにも削除の印を付けない（追加・更新だけ）。"""
    root = Path(root)
    res = SaveResult()
    backend.ensure()
    local = scan(root, paths, excludes)
    res.scanned = len(local)
    latest = latest_versions(r for _, r in backend.iter_meta())
    base, base_at = read_baseline(baseline_path) if baseline_path else (None, None)
    plan = plan_changes(local, latest, base, base_at, scope_paths=paths, excludes=excludes)
    res.skipped_newer = plan["skipped_newer"]
    if not delete_missing:
        plan["delete"] = []
    if not plan["upload"] and not plan["delete"]:
        return res
    saved_at = _now_iso()
    entries = [(p, local[p]["sha256"]) for p in plan["upload"]] + [(p, "") for p in plan["delete"]]
    res.version_id = vid = make_version_id(source, run_id, entries, saved_at)
    work = Path(tempfile.mkdtemp(prefix="state_store_", dir=str(tmpdir) if tmpdir else None))
    batch_no, cur_bytes, fh, cur_path = 0, 0, None, None

    def _flush():
        nonlocal fh, cur_path, batch_no, cur_bytes
        if fh is None:
            return True
        fh.close()
        ok = backend.load_file(cur_path, f"st_{vid}_b{batch_no}")
        res.jobs += 1
        cur_path.unlink(missing_ok=True)
        fh, cur_path, cur_bytes = None, None, 0
        batch_no += 1
        return ok

    try:
        for rel in plan["upload"] + plan["delete"]:
            data = None
            if rel in local:
                try:
                    data = (root / rel).read_bytes()
                except OSError:
                    continue
                # 走査の後に書き換わったら、読んだ中身の sha を記録する
                res.bytes_raw += len(data)
            rows = build_rows(rel, data, vid, saved_at, run_id, source, chunk_bytes)
            if fh is None:
                cur_path = work / f"batch_{batch_no}.ndjson"
                fh = open(cur_path, "w", encoding="utf-8")
            for r in rows:
                line = json.dumps(r, ensure_ascii=False)
                fh.write(line + "\n")
                cur_bytes += len(line) + 1
                if r.get("content"):
                    res.bytes_gz += len(r["content"]) * 3 // 4
            if data is None:
                res.deleted += 1
            else:
                res.uploaded += 1
            if cur_bytes >= batch_bytes:
                res.ok = _flush() and res.ok
        res.ok = _flush() and res.ok
    finally:
        if fh is not None:
            fh.close()
        for p in work.glob("*"):
            p.unlink(missing_ok=True)
        work.rmdir()
    return res


def save_scoped(root: Path, paths, *, source: str, delete_missing: bool = True, cfg=None,
                backend=None) -> SaveResult:
    """Actions の対象（STATE_STORE_PATHS）の外に置くもの（構成管理資料・投資部の部則など）を基準なしで保存する。
    削除の印は paths の範囲の中だけに付く（範囲外の版には触れない）。BQ の認証が無ければ例外のまま返す。"""
    cfg = cfg if cfg is not None else load_config()
    backend = backend if backend is not None else BQBackend(cfg)
    return save(backend, root, tuple(paths), tuple(getattr(cfg, "STATE_STORE_EXCLUDE", ())), source=source,
                chunk_bytes=int(getattr(cfg, "STATE_STORE_CHUNK_BYTES", 4 * 1024 ** 2)),
                batch_bytes=int(getattr(cfg, "STATE_STORE_LOAD_BATCH_BYTES", 256 * 1024 ** 2)),
                delete_missing=delete_missing)


@dataclass
class RestoreResult:
    paths: int = 0
    written: int = 0
    unchanged: int = 0
    removed: int = 0
    bad: int = 0
    bytes_raw: int = 0
    mode: str = ""
    files: dict = field(default_factory=dict)


def _write_atomic(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".state_store_tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def restore(backend, root: Path, prefixes=None, *, strip: str = "", remove_deleted: bool = False,
            excludes=()) -> RestoreResult:
    """パスごとに最新の版を root に書き戻す。prefixes で対象を絞る（前方一致）。
    手元の sha が同じファイルは書かない。変わったパスが少なければ行番号で1行ずつ取り出す。"""
    root = Path(root)
    res = RestoreResult()
    want = (lambda p: any(p.startswith(x) for x in prefixes)) if prefixes else (lambda p: True)
    meta = [(i, r) for i, r in backend.iter_meta()]
    latest = latest_versions(r for _, r in meta)
    targets = {p: v for p, v in latest.items() if want(p) and not is_excluded(p, excludes)}
    res.paths = sum(1 for v in targets.values() if not v["deleted"])
    need = {}
    for p, v in targets.items():
        dst = dest_for(p, root, strip)
        if v["deleted"]:
            if remove_deleted and dst.is_file():
                dst.unlink()
                res.removed += 1
            continue
        if dst.is_file() and sha256_file(dst) == v["sha256"]:
            res.unchanged += 1
            res.files[p] = v["sha256"]
            continue
        need[p] = v
    if not need:
        res.mode = "none"
        return res
    # 必要な行の行番号（表の並び）
    idx = {}
    for i, r in meta:
        v = need.get(r.get("path"))
        if v is not None and r.get("version_id") == v["version_id"]:
            idx[(r["path"], r.get("part") or 0)] = i
    parts: dict = {p: {} for p in need}
    n_rows = len(idx)
    use_index = n_rows <= INDEXED_FETCH_MAX
    if use_index:
        res.mode = "indexed"
        for (p, part), i in sorted(idx.items(), key=lambda kv: kv[1]):
            r = backend.fetch_row(i)
            if not r or r.get("path") != p or r.get("version_id") != need[p]["version_id"] \
                    or (r.get("part") or 0) != part:
                use_index = False   # 表の並びが変わった（掃除・追記の途中）。全件読みへ切り替える
                break
            parts[p][part] = _content_bytes(r.get("content"))
    if not use_index and hasattr(backend, "iter_spans"):
        # 要る行のまとまりだけを読む（除外した大きなフォルダの行を読まずに済む）。並びが変わっていたら全件読みへ
        res.mode = "spans"
        parts = {p: {} for p in need}
        want_at = {i: key for key, i in idx.items()}
        for i, r in backend.iter_spans(row_spans(sorted(want_at))):
            key = want_at.get(i)
            if key is None:
                continue
            p, part = key
            if r.get("path") != p or r.get("version_id") != need[p]["version_id"] or (r.get("part") or 0) != part:
                res.mode = "full"
                break
            parts[p][part] = _content_bytes(r.get("content"))
        use_index = res.mode == "spans"
    if not use_index:
        res.mode = "full"
        parts = {p: {} for p in need}
        for _, r in backend.iter_full():
            p = r.get("path")
            v = need.get(p)
            if v is not None and r.get("version_id") == v["version_id"]:
                parts[p][r.get("part") or 0] = _content_bytes(r.get("content"))
    for p, v in need.items():
        data = assemble(parts[p], int(v["parts"] or 0), v["sha256"])
        if data is None:
            res.bad += 1
            continue
        _write_atomic(dest_for(p, root, strip), data)
        res.written += 1
        res.bytes_raw += len(data)
        res.files[p] = v["sha256"]
    return res


PRUNE_SQL = """
MERGE `{table}` T
USING (
  SELECT path, version_id FROM (
    SELECT path, version_id,
           DENSE_RANK() OVER (PARTITION BY path ORDER BY saved_at DESC, version_id DESC) AS rk
    FROM (SELECT DISTINCT path, version_id, saved_at FROM `{table}`)
  ) WHERE rk > @keep
) S
ON T.path = S.path AND T.version_id = S.version_id
WHEN MATCHED THEN DELETE
"""


def prune(backend, keep: int, max_bytes: int, dry_run_only: bool = False) -> dict:
    """パスごとに新しい版を keep 個だけ残す。MERGE…DELETE は追記中の load job と競合しても
    この回が失敗するだけで、新しい版は消さない（照合するのは古い版の path・version_id だけ）。"""
    from google.cloud import bigquery
    sql = PRUNE_SQL.format(table=backend.table_ref)
    params = [bigquery.ScalarQueryParameter("keep", "INT64", int(keep))]
    dry = backend.query(sql, params, dry_run=True, max_bytes=max_bytes)
    est = int(dry.total_bytes_processed or 0)
    out = {"estimated_bytes": est, "ran": False, "deleted_rows": 0}
    if dry_run_only:
        return out
    if est > max_bytes:
        out["refused"] = True
        return out
    job = backend.query(sql, params, dry_run=False, max_bytes=max_bytes)
    out.update(ran=True, deleted_rows=int(job.num_dml_affected_rows or 0),
               billed_bytes=int(job.total_bytes_billed or 0))
    return out


# ── CLI ──────────────────────────────────────────────────────

def _mb(n: int) -> str:
    return f"{n / 1024 ** 2:.1f}MB"


def _baseline_path(args) -> Path:
    return Path(args.baseline or os.environ.get("STATE_STORE_BASELINE") or (Path(args.root) / BASELINE_NAME))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="状態ファイルを BigQuery に置く（load job と tabledata.list だけ）")
    ap.add_argument("command", choices=["prepare", "save", "restore", "baseline", "prune", "info"])
    ap.add_argument("--root", default=str(REPO_ROOT), help="リポジトリ直下に当たるフォルダ")
    ap.add_argument("--paths", nargs="*", help="対象（既定は config.STATE_STORE_PATHS）")
    ap.add_argument("--exclude", nargs="*", default=[], help="この回だけ足す除外（前方一致）")
    ap.add_argument("--prefix", nargs="*", help="restore で戻すパスの前方一致")
    ap.add_argument("--strip", default="", help="restore で書き戻す先から外す先頭（例 stock_data/）")
    ap.add_argument("--remove-deleted", action="store_true", help="restore で削除の印のあるファイルを手元からも消す")
    ap.add_argument("--source", default="", help="保存元の名前（workflow 名など）")
    ap.add_argument("--run-id", default="", help="実行 ID（既定は GITHUB_RUN_ID-GITHUB_RUN_ATTEMPT）")
    ap.add_argument("--baseline", default="", help="基準ファイル（既定 <root>/.state_store_baseline.json）")
    ap.add_argument("--no-baseline", action="store_true", help="save で基準を使わない")
    ap.add_argument("--keep", type=int, default=0, help="prune で残す版の数")
    ap.add_argument("--dry-run", action="store_true", help="prune の見積もりだけ")
    ap.add_argument("--force", action="store_true", help="モードが git でも実行する")
    ap.add_argument("--profile", choices=["fetch"], help="fetch＝取得系の workflow。config.STATE_STORE_EXCLUDE_FETCH も除外する（読み書きしない）")
    a = ap.parse_args(argv)
    cfg = load_config()
    mode = resolve_mode(cfg)
    root = Path(a.root)
    paths = tuple(a.paths) if a.paths else tuple(getattr(cfg, "STATE_STORE_PATHS", ()))
    excludes = tuple(getattr(cfg, "STATE_STORE_EXCLUDE", ())) + tuple(a.exclude or ())
    if a.profile == "fetch":
        excludes += tuple(getattr(cfg, "STATE_STORE_EXCLUDE_FETCH", ()))
    t0 = time.time()

    if a.command == "baseline" or (a.command == "prepare" and mode == "dual"):
        files = scan(root, paths, excludes)
        write_baseline(_baseline_path(a), files)
        print(f"✅ state_store baseline: {len(files)}ファイル（{time.time() - t0:.1f}秒）")
        return 0
    if a.command == "prepare" and mode == "git":
        print("⏩ state_store prepare: STATE_STORE_MODE=git（何もしない）")
        return 0
    if a.command == "save" and mode == "git" and not a.force:
        print("⏩ state_store save: STATE_STORE_MODE=git（BQ に保存しない）")
        return 0

    if a.command == "save" and a.paths:
        # 手で範囲を渡す save（クラウドで直した資料を戻すなど）で、その範囲が手元に1件も無いのは
        # restore を忘れた状態。そのまま保存すると範囲の全ファイルに削除の印が付くので止める
        empty = [p for p in paths if not scan(root, (p,), excludes)]
        if empty:
            print(f"❌ state_store save: 手元に1件も無い範囲がある（{len(empty)}件）。先に restore するか --paths を見直す")
            return 1

    backend = BQBackend(cfg)
    if a.command in ("restore", "prepare"):
        backend.ensure()
        prefixes = a.prefix or (list(paths) if a.command == "prepare" else None)
        r = restore(backend, root, prefixes, strip=a.strip,
                    remove_deleted=a.remove_deleted or a.command == "prepare", excludes=excludes)
        if a.command == "prepare":
            write_baseline(_baseline_path(a), scan(root, paths, excludes), created_at=t0)
        print(f"{'✅' if not r.bad else '❌'} state_store restore: 対象{r.paths}・書き戻し{r.written}"
              f"（{_mb(r.bytes_raw)}）・同じ{r.unchanged}・削除{r.removed}・不整合{r.bad}"
              f"・読み方 {r.mode}（{time.time() - t0:.1f}秒）")
        return 1 if r.bad else 0
    if a.command == "save":
        run_id = a.run_id or "-".join(x for x in (os.environ.get("GITHUB_RUN_ID", ""),
                                                  os.environ.get("GITHUB_RUN_ATTEMPT", "")) if x)
        bl = None if a.no_baseline else _baseline_path(a)
        r = save(backend, root, paths, excludes, source=a.source or "manual", run_id=run_id,
                 baseline_path=bl if (bl and bl.exists()) else None,
                 chunk_bytes=int(getattr(cfg, "STATE_STORE_CHUNK_BYTES", 4 * 1024 ** 2)),
                 batch_bytes=int(getattr(cfg, "STATE_STORE_LOAD_BATCH_BYTES", 256 * 1024 ** 2)))
        print(f"{'✅' if r.ok else '❌'} state_store save（{mode}）: 走査{r.scanned}・保存{r.uploaded}"
              f"（{_mb(r.bytes_raw)}→gzip {_mb(r.bytes_gz)}）・削除の印{r.deleted}"
              f"・他の実行が新しいため見送り{r.skipped_newer}・load job {r.jobs}本（{time.time() - t0:.1f}秒）")
        return 0 if r.ok else 1
    if a.command == "prune":
        backend.ensure()
        keep = a.keep or int(getattr(cfg, "STATE_STORE_KEEP_VERSIONS", 3))
        mx = int(getattr(cfg, "STATE_STORE_PRUNE_MAX_BYTES", 1024 ** 3))
        r = prune(backend, keep, mx, dry_run_only=a.dry_run)
        if r.get("refused"):
            print(f"❌ state_store prune: 見積もり {_mb(r['estimated_bytes'])} が上限 {_mb(mx)} を超えるため実行しない")
            return 1
        print(f"✅ state_store prune（{keep}版を残す）: 見積もり {_mb(r['estimated_bytes'])}"
              + (f"・削除 {r['deleted_rows']}行・課金対象 {_mb(r.get('billed_bytes', 0))}" if r["ran"] else "・dry-run"))
        return 0
    if a.command == "info":
        info = backend.table_info()
        print(f"state_store: {info['num_rows']}行・{_mb(int(info['num_bytes'] or 0))}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
