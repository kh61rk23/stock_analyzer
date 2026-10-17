"""DeepL へのキーは Authorization ヘッダーで送る（本文の auth_key は 403 になる）。"""
import io
import json

import business_summary_fetcher as bsf


def test_translate_sends_key_in_header(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = req.data.decode()
        return io.BytesIO(json.dumps({"translations": [{"text": "訳"}]}).encode())

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bsf, "_deepl_disabled_this_run", False)
    fn = next(getattr(bsf, n) for n in dir(bsf) if "translate" in n.lower() and callable(getattr(bsf, n)))
    out = fn("Toyota makes cars. It is large.", "abc:fx")
    assert out == "訳" and seen["auth"] == "DeepL-Auth-Key abc:fx" and "auth_key" not in seen["body"]
