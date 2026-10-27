-- Quota as reported by the provider CLI itself (e.g. Claude Code rate_limit_event):
-- {"source": "...", "windows": {"five_hour": {"used_pct": 12.0, "resets_at": "...", "status": "allowed"}, ...}}
ALTER TABLE agents ADD COLUMN provider_quota_json TEXT;
ALTER TABLE agents ADD COLUMN provider_quota_at TEXT;
