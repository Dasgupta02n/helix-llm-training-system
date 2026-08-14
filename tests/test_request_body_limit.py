"""Uploads advertise 25 MiB; the HTTP body cap must not reject those zips."""

from helix.services.user_gold_upload import MAX_ZIP_BYTES
from helix.services.user_material_upload import MAX_ZIP_BYTES as MATERIAL_ZIP_MAX


def test_http_body_limit_allows_advertised_zip_uploads():
    from helix.config import Settings

    s = Settings(max_request_body_bytes=28 * 1024 * 1024)
    assert s.max_request_body_bytes >= MAX_ZIP_BYTES
    assert s.max_request_body_bytes >= MATERIAL_ZIP_MAX
    # Default in config.py (ignore a local .env override)
    from helix import config as cfg

    assert cfg.Settings.model_fields["max_request_body_bytes"].default >= MAX_ZIP_BYTES
