SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id        TEXT PRIMARY KEY,
    rin            TEXT,
    docket_id      TEXT,
    agency         TEXT,
    title          TEXT,
    proposed_doc   TEXT,
    final_doc      TEXT,
    proposed_date  TEXT,
    final_date     TEXT
);

CREATE TABLE IF NOT EXISTS provisions (
    provision_id   TEXT PRIMARY KEY,
    cfr_title      INTEGER NOT NULL,
    cfr_part       TEXT NOT NULL,
    section        TEXT NOT NULL,
    UNIQUE (cfr_title, cfr_part, section)
);

CREATE TABLE IF NOT EXISTS provision_versions (
    rule_id        TEXT NOT NULL,
    doc_type       TEXT NOT NULL CHECK (doc_type IN ('proposed','final')),
    provision_id   TEXT NOT NULL,
    heading        TEXT,
    body           TEXT NOT NULL,
    char_count     INTEGER NOT NULL,
    PRIMARY KEY (rule_id, doc_type, provision_id)
);

CREATE TABLE IF NOT EXISTS amendments (
    rule_id        TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    provision_id   TEXT NOT NULL,
    verb           TEXT NOT NULL,
    instruction    TEXT NOT NULL,
    PRIMARY KEY (rule_id, doc_type, provision_id, verb)
);

CREATE TABLE IF NOT EXISTS orgs (
    org_id         TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    sector         TEXT NOT NULL CHECK (sector IN ('industry','advocacy','government','individual','unknown'))
);

CREATE TABLE IF NOT EXISTS org_aliases (
    alias          TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id     TEXT PRIMARY KEY,
    rule_id        TEXT,
    docket_id      TEXT NOT NULL,
    org_id         TEXT,
    posted_date    TEXT,
    title          TEXT,
    text_status    TEXT NOT NULL,
    word_count     INTEGER NOT NULL,
    source_url     TEXT NOT NULL,
    body           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asks (
    ask_id         TEXT PRIMARY KEY,
    comment_id     TEXT NOT NULL,
    rule_id        TEXT NOT NULL,
    provision_id   TEXT NOT NULL,
    paragraph      TEXT,
    direction      TEXT NOT NULL CHECK (direction IN ('retain','delete','revise','add','unclear')),
    ask_type       TEXT NOT NULL CHECK (ask_type IN ('technical','substantive','unclear')),
    request_text   TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('granted','denied','untouched','inconclusive','unreviewed')),
    evidence       TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
    meeting_id     TEXT PRIMARY KEY,
    rin            TEXT NOT NULL,
    meeting_date   TEXT NOT NULL,
    stage          TEXT,
    source_url     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting_attendees (
    meeting_id     TEXT NOT NULL,
    org_id         TEXT NOT NULL,
    person         TEXT NOT NULL,
    PRIMARY KEY (meeting_id, org_id, person)
);

CREATE TABLE IF NOT EXISTS dispositions (
    rule_id        TEXT NOT NULL,
    ordinal        INTEGER NOT NULL,
    disposition    TEXT NOT NULL,
    comment_summary TEXT NOT NULL,
    epa_response   TEXT NOT NULL,
    PRIMARY KEY (rule_id, ordinal)
);

CREATE INDEX IF NOT EXISTS pv_by_provision ON provision_versions (provision_id);
CREATE INDEX IF NOT EXISTS asks_by_provision ON asks (provision_id);
CREATE INDEX IF NOT EXISTS asks_by_comment ON asks (comment_id);
CREATE INDEX IF NOT EXISTS amd_by_provision ON amendments (provision_id);
CREATE INDEX IF NOT EXISTS att_by_org ON meeting_attendees (org_id);
"""

PROVISION_HISTORY_VIEW = """
DROP VIEW IF EXISTS provision_history;
CREATE VIEW provision_history AS
SELECT p.provision_id, p.cfr_title, p.cfr_part, p.section,
       r.rule_id, r.title AS rule_title, r.final_date,
       a.verb, pv.char_count,
       (SELECT COUNT(*) FROM asks k WHERE k.provision_id = p.provision_id AND k.rule_id = r.rule_id) AS asks_filed
FROM provisions p
JOIN provision_versions pv ON pv.provision_id = p.provision_id
JOIN rules r ON r.rule_id = pv.rule_id
LEFT JOIN amendments a ON a.provision_id = p.provision_id AND a.rule_id = r.rule_id AND a.doc_type = pv.doc_type
WHERE pv.doc_type = 'final';
"""
