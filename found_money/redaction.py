import hashlib
import json
import re


def token(source_id):
    return "rec_" + hashlib.sha256(str(source_id).encode()).hexdigest()[:12]


def assert_public_safe(payload):
    # Preserve Unicode so an em dash (JSON ``\u2014``) beside an amount cannot
    # be misread as a phone-like digit sequence by the public-safety scanner.
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    pattern = r'(?i)("(?:email|phone|name|company|source_id|record_id|contact_id|crm_url|deal_name)"\s*:|https?://|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\+?\d[\d .()/-]{7,})'
    if re.search(pattern, text):
        raise ValueError("public output contains a sensitive identifier or URL")
    return True
