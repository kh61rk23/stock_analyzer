from setup_db_integration_bq import _statements


def test_schema_migration_is_idempotent_and_covers_w3_w5():
    ddl = "\n".join(_statements("test-project"))
    assert "CREATE SCHEMA IF NOT EXISTS `test-project.real_estate`" in ddl
    assert "CREATE TABLE IF NOT EXISTS `test-project.edinet.company`" in ddl
    assert "ADD COLUMN IF NOT EXISTS edinet_code" in ddl
    assert "ADD COLUMN IF NOT EXISTS raw_text" in ddl
    assert "real_estate.corporate_holdings_p1" in ddl
    assert "real_estate.area_profile_p1" in ddl
