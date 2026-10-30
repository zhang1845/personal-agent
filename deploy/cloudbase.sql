-- Daynest's bounded snapshot store. Apply once through the authenticated
-- CloudBase SQL editor. Does not inspect or change unrelated application data.
-- Never put a model key, owner password, API key or environment ID in this file.
BEGIN;
CREATE SCHEMA IF NOT EXISTS daynest_private;
REVOKE ALL ON SCHEMA daynest_private FROM PUBLIC, anon, authenticated;

CREATE TABLE IF NOT EXISTS daynest_private.state (
    name text PRIMARY KEY,
    version bigint NOT NULL CHECK (version > 0),
    payload text NOT NULL CHECK (length(payload) BETWEEN 1 AND 4194304),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (name IN ('calendar.sqlite3','assistant.sqlite3','knowledge.sqlite3',
        'automations.sqlite3','caldav.sqlite3','agent-runs.sqlite3',
        'chat-integrations.sqlite3','quality.sqlite3','cloud-auth.sqlite3',
        'model-api.json','notifications.json','caldav.json'))
);
CREATE TABLE IF NOT EXISTS daynest_private.lease (
    singleton smallint PRIMARY KEY CHECK (singleton = 1),
    owner text,
    expires_at timestamptz NOT NULL,
    initialized boolean NOT NULL DEFAULT false
);
ALTER TABLE daynest_private.lease ADD COLUMN IF NOT EXISTS initialized boolean NOT NULL DEFAULT false;
ALTER TABLE daynest_private.state ENABLE ROW LEVEL SECURITY;
ALTER TABLE daynest_private.lease ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA daynest_private FROM PUBLIC, anon, authenticated, service_role;

-- CloudBase's REST gateway may ignore GRANT EXECUTE for RPCs. Every externally
-- callable function therefore checks auth.role() internally. SECURITY DEFINER
-- accesses only these two private tables, with an explicit trusted search path.
CREATE OR REPLACE FUNCTION daynest_private.require_service()
RETURNS void LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RAISE EXCEPTION 'Daynest backend authentication required' USING ERRCODE = '42501';
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION daynest_private.require_service() FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION daynest_private.require_owner(p_owner text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
BEGIN
    PERFORM daynest_private.require_service();
    IF p_owner IS NULL OR p_owner !~ '^[A-Za-z0-9_-]{32,128}$' THEN
        RAISE EXCEPTION 'Daynest request lease required' USING ERRCODE = '42501';
    END IF;
    -- Serialize with claim/release as well as state mutations in this RPC.
    PERFORM 1 FROM daynest_private.lease
        WHERE singleton=1 AND owner=p_owner AND expires_at>clock_timestamp()
        FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Daynest request lease expired' USING ERRCODE = '42501';
    END IF;
END;
$$;
REVOKE ALL ON FUNCTION daynest_private.require_owner(text) FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION public.daynest_lease_claim(p_owner text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
DECLARE claimed text;
BEGIN
    PERFORM daynest_private.require_service();
    IF p_owner IS NULL OR p_owner !~ '^[A-Za-z0-9_-]{32,128}$' THEN
        RAISE EXCEPTION 'Invalid Daynest owner' USING ERRCODE = '22023';
    END IF;
    INSERT INTO daynest_private.lease(singleton,owner,expires_at)
        VALUES(1,p_owner,clock_timestamp()+interval '600 seconds')
        ON CONFLICT(singleton) DO UPDATE
        SET owner=EXCLUDED.owner,expires_at=clock_timestamp()+interval '600 seconds'
        WHERE daynest_private.lease.owner IS NULL
           OR daynest_private.lease.expires_at<=clock_timestamp()
        RETURNING owner INTO claimed;
    RETURN claimed IS NOT NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.daynest_lease_release(p_owner text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
BEGIN
    PERFORM daynest_private.require_service();
    IF p_owner IS NULL OR p_owner !~ '^[A-Za-z0-9_-]{32,128}$' THEN
        RAISE EXCEPTION 'Invalid Daynest owner' USING ERRCODE = '22023';
    END IF;
    UPDATE daynest_private.lease SET owner=NULL,expires_at='epoch'::timestamptz
        WHERE singleton=1 AND owner=p_owner;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION public.daynest_lease_check(p_owner text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
BEGIN
    PERFORM daynest_private.require_service();
    IF p_owner IS NULL OR p_owner !~ '^[A-Za-z0-9_-]{32,128}$' THEN
        RETURN false;
    END IF;
    RETURN EXISTS(SELECT 1 FROM daynest_private.lease
        WHERE singleton=1 AND owner=p_owner AND expires_at>clock_timestamp());
END;
$$;

CREATE OR REPLACE FUNCTION public.daynest_lease_complete_init(p_owner text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
BEGIN
    PERFORM daynest_private.require_owner(p_owner);
    IF (SELECT count(*) FROM daynest_private.state WHERE version>=1 AND name IN (
        'calendar.sqlite3','assistant.sqlite3','knowledge.sqlite3','automations.sqlite3',
        'caldav.sqlite3','agent-runs.sqlite3','chat-integrations.sqlite3',
        'quality.sqlite3','cloud-auth.sqlite3'))<>9 THEN
        RAISE EXCEPTION 'Daynest initialization incomplete' USING ERRCODE = '55000';
    END IF;
    UPDATE daynest_private.lease SET initialized=true WHERE singleton=1;
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION public.daynest_state_read(p_name text,p_owner text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
DECLARE item daynest_private.state%ROWTYPE;
BEGIN
    PERFORM daynest_private.require_owner(p_owner);
    IF p_name IS NULL OR p_name NOT IN ('calendar.sqlite3','assistant.sqlite3','knowledge.sqlite3',
        'automations.sqlite3','caldav.sqlite3','agent-runs.sqlite3',
        'chat-integrations.sqlite3','quality.sqlite3','cloud-auth.sqlite3',
        'model-api.json','notifications.json','caldav.json') THEN
        RAISE EXCEPTION 'Invalid Daynest record' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO item FROM daynest_private.state WHERE name=p_name;
    IF NOT FOUND THEN
        IF p_name LIKE '%.sqlite3' AND (SELECT initialized FROM daynest_private.lease WHERE singleton=1) THEN
            RAISE EXCEPTION 'Daynest database missing; refusing empty fallback' USING ERRCODE = '55000';
        END IF;
        RETURN jsonb_build_object('version',0,'payload',NULL,'digest',NULL);
    END IF;
    RETURN jsonb_build_object('version',item.version,'payload',item.payload,'digest',item.digest);
END;
$$;

CREATE OR REPLACE FUNCTION public.daynest_state_cas(
    p_name text,p_expected bigint,p_payload text,p_digest text,p_owner text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, daynest_private, pg_temp AS $$
DECLARE saved_version bigint;
BEGIN
    PERFORM daynest_private.require_owner(p_owner);
    IF p_name IS NULL OR p_name NOT IN ('calendar.sqlite3','assistant.sqlite3','knowledge.sqlite3',
        'automations.sqlite3','caldav.sqlite3','agent-runs.sqlite3',
        'chat-integrations.sqlite3','quality.sqlite3','cloud-auth.sqlite3',
        'model-api.json','notifications.json','caldav.json')
        OR p_expected IS NULL OR p_expected<0 OR p_expected>=9007199254740992
        OR p_payload IS NULL OR length(p_payload) NOT BETWEEN 1 AND 4194304
        OR p_payload !~ '^[A-Za-z0-9+/]+={0,2}$'
        OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Invalid Daynest state' USING ERRCODE = '22023';
    END IF;
    IF p_expected=0 THEN
        IF p_name LIKE '%.sqlite3' AND (SELECT initialized FROM daynest_private.lease WHERE singleton=1) THEN
            RAISE EXCEPTION 'Daynest database recreation requires explicit recovery' USING ERRCODE = '55000';
        END IF;
        INSERT INTO daynest_private.state(name,version,payload,digest)
            VALUES(p_name,1,p_payload,p_digest)
            ON CONFLICT(name) DO NOTHING RETURNING version INTO saved_version;
    ELSE
        UPDATE daynest_private.state
            SET version=version+1,payload=p_payload,digest=p_digest,updated_at=clock_timestamp()
            WHERE name=p_name AND version=p_expected
            RETURNING version INTO saved_version;
    END IF;
    IF saved_version IS NULL THEN
        RETURN jsonb_build_object('ok',false);
    END IF;
    RETURN jsonb_build_object('ok',true,'version',saved_version);
END;
$$;

REVOKE ALL ON FUNCTION public.daynest_lease_claim(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.daynest_lease_release(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.daynest_lease_check(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.daynest_lease_complete_init(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.daynest_state_read(text,text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.daynest_state_cas(text,bigint,text,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.daynest_lease_claim(text) TO service_role;
GRANT EXECUTE ON FUNCTION public.daynest_lease_release(text) TO service_role;
GRANT EXECUTE ON FUNCTION public.daynest_lease_check(text) TO service_role;
GRANT EXECUTE ON FUNCTION public.daynest_lease_complete_init(text) TO service_role;
GRANT EXECUTE ON FUNCTION public.daynest_state_read(text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.daynest_state_cas(text,bigint,text,text,text) TO service_role;
COMMIT;
