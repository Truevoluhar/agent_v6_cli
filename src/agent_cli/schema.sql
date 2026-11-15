-- Run once as deployment administrator. Does not create/reset existing application users.
CREATE SCHEMA IF NOT EXISTS agent_cli;
REVOKE ALL ON SCHEMA agent_cli FROM PUBLIC;
CREATE TABLE IF NOT EXISTS agent_cli.threads (
 id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES public.app_users(id), title TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS agent_cli.messages (
 id BIGSERIAL PRIMARY KEY,thread_id UUID NOT NULL REFERENCES agent_cli.threads(id),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),metadata JSONB NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS cli_messages_thread ON agent_cli.messages(thread_id,id);
CREATE TABLE IF NOT EXISTS agent_cli.memories (
 user_id UUID PRIMARY KEY REFERENCES public.app_users(id),content TEXT NOT NULL DEFAULT '',version BIGINT NOT NULL DEFAULT 1,updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS agent_cli.turns (
 id UUID PRIMARY KEY,thread_id UUID NOT NULL REFERENCES agent_cli.threads(id),user_id UUID NOT NULL REFERENCES public.app_users(id),
 status TEXT NOT NULL DEFAULT 'running',created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 steps JSONB NOT NULL DEFAULT '[]',error TEXT NOT NULL DEFAULT '');
REVOKE ALL ON ALL TABLES IN SCHEMA agent_cli FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA agent_cli FROM PUBLIC;

CREATE OR REPLACE FUNCTION agent_cli.rpc(action TEXT, token TEXT, data JSONB DEFAULT '{}') RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,public AS $$
DECLARE u UUID; uname TEXT; urole TEXT; tid UUID; newtoken TEXT; outdata JSONB; n BIGINT;
BEGIN
 IF action='login' THEN
   SELECT id,username,role INTO u,uname,urole FROM public.app_users
    WHERE lower(username)=lower(trim(data->>'username')) AND password_plain=data->>'password' AND is_active;
   IF u IS NULL THEN RAISE EXCEPTION 'Invalid credentials' USING ERRCODE='28000'; END IF;
   newtoken:=gen_random_uuid()::text||gen_random_uuid()::text;
   INSERT INTO public.auth_sessions(guid,user_id,expires_at) VALUES(newtoken,u,now()+interval '12 hours');
   RETURN jsonb_build_object('guid',newtoken,'user_id',u,'username',uname,'role',urole);
 END IF;
 SELECT a.id,a.username,a.role INTO u,uname,urole FROM public.auth_sessions s JOIN public.app_users a ON a.id=s.user_id
  WHERE s.guid=token AND s.revoked_at IS NULL AND s.expires_at>now() AND a.is_active;
 IF u IS NULL THEN RAISE EXCEPTION 'Session invalid or expired; login again' USING ERRCODE='28000'; END IF;
 IF action='me' THEN RETURN jsonb_build_object('user_id',u,'username',uname,'role',urole);
 ELSIF action='logout' THEN UPDATE public.auth_sessions SET revoked_at=now() WHERE guid=token; RETURN '{}';
 ELSIF action='threads' THEN
  SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.updated_at DESC),'[]') INTO outdata FROM agent_cli.threads t WHERE user_id=u; RETURN outdata;
 ELSIF action='new_thread' THEN
  tid:=gen_random_uuid();INSERT INTO agent_cli.threads(id,user_id,title) VALUES(tid,u,coalesce(data->>'title',''));
  RETURN jsonb_build_object('id',tid);
 ELSIF action='memory_get' THEN
  SELECT jsonb_build_object('content',content,'version',version) INTO outdata FROM agent_cli.memories WHERE user_id=u;
  RETURN coalesce(outdata,'{"content":"","version":0}');
 ELSIF action='memory_set' THEN
  IF (data->>'version')::bigint=0 THEN
   INSERT INTO agent_cli.memories(user_id,content) VALUES(u,data->>'content') ON CONFLICT DO NOTHING; GET DIAGNOSTICS n=ROW_COUNT;
  ELSE
   UPDATE agent_cli.memories SET content=data->>'content',version=version+1,updated_at=now() WHERE user_id=u AND version=(data->>'version')::bigint;
   GET DIAGNOSTICS n=ROW_COUNT;
  END IF;
  IF n<>1 THEN RAISE EXCEPTION 'Memory changed concurrently; reload before writing' USING ERRCODE='40001'; END IF;
  RETURN jsonb_build_object('saved',true);
 END IF;
 tid:=(data->>'thread_id')::uuid;
 IF NOT EXISTS(SELECT 1 FROM agent_cli.threads WHERE id=tid AND user_id=u) THEN
  RAISE EXCEPTION 'Thread not found' USING ERRCODE='42501';
 END IF;
 IF action='turns' THEN
  SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'status',status,'created_at',created_at,'updated_at',updated_at,'error',error) ORDER BY created_at DESC),'[]') INTO outdata FROM agent_cli.turns WHERE thread_id=tid; RETURN outdata;
 ELSIF action='messages' THEN
  SELECT coalesce(jsonb_agg(to_jsonb(m) ORDER BY m.id),'[]') INTO outdata FROM agent_cli.messages m WHERE thread_id=tid; RETURN outdata;
 ELSIF action='begin_turn' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(u::text,71935));
  PERFORM 1 FROM agent_cli.threads WHERE id=tid FOR UPDATE;
  IF EXISTS(SELECT 1 FROM agent_cli.turns WHERE user_id=u AND status='running' AND updated_at>now()-interval '5 minutes') THEN
   RAISE EXCEPTION 'Another CLI turn is still running for this account' USING ERRCODE='55000';
  END IF;
  UPDATE agent_cli.turns SET status='interrupted',error='Client heartbeat expired' WHERE user_id=u AND status='running';
  INSERT INTO agent_cli.turns(id,thread_id,user_id) VALUES((data->>'turn_id')::uuid,tid,u);
  INSERT INTO agent_cli.messages(thread_id,role,content,metadata) VALUES(tid,'user',data->>'message',coalesce(data->'metadata','{}'));
  UPDATE agent_cli.threads SET title=CASE WHEN title='' THEN left(data->>'message',80) ELSE title END,updated_at=now() WHERE id=tid;
  RETURN '{}';
 ELSIF action IN ('turn_step','heartbeat','finish_turn','fail_turn','turn') THEN
  IF NOT EXISTS(SELECT 1 FROM agent_cli.turns WHERE id=(data->>'turn_id')::uuid AND thread_id=tid AND user_id=u) THEN
   RAISE EXCEPTION 'Turn not found' USING ERRCODE='42501';END IF;
  IF action='turn' THEN SELECT to_jsonb(t) INTO outdata FROM agent_cli.turns t WHERE id=(data->>'turn_id')::uuid; RETURN outdata;END IF;
  IF action='heartbeat' THEN UPDATE agent_cli.turns SET updated_at=now() WHERE id=(data->>'turn_id')::uuid AND status='running'; RETURN '{}';END IF;
  IF action='turn_step' THEN
   UPDATE agent_cli.turns SET steps=steps||jsonb_build_array(data->'step'),updated_at=now() WHERE id=(data->>'turn_id')::uuid AND status='running';
  ELSIF action='finish_turn' THEN
   UPDATE agent_cli.turns SET status='completed',updated_at=now() WHERE id=(data->>'turn_id')::uuid AND status='running';
   GET DIAGNOSTICS n=ROW_COUNT;
   IF n<>1 THEN RAISE EXCEPTION 'Turn is no longer running' USING ERRCODE='55000';END IF;
   INSERT INTO agent_cli.messages(thread_id,role,content,metadata) VALUES(tid,'assistant',data->>'content',coalesce(data->'metadata','{}'));
   UPDATE agent_cli.threads SET updated_at=now() WHERE id=tid;
  ELSE UPDATE agent_cli.turns SET status='failed',error=data->>'error',updated_at=now() WHERE id=(data->>'turn_id')::uuid AND status='running';END IF;
  RETURN '{}';
 END IF;
 RAISE EXCEPTION 'Unknown operation' USING ERRCODE='22023';
END $$;
REVOKE ALL ON FUNCTION agent_cli.rpc(TEXT,TEXT,JSONB) FROM PUBLIC;
DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='agent_cli_client') THEN CREATE ROLE agent_cli_client NOLOGIN;END IF;END $$;
GRANT USAGE ON SCHEMA agent_cli TO agent_cli_client;
GRANT EXECUTE ON FUNCTION agent_cli.rpc(TEXT,TEXT,JSONB) TO agent_cli_client;
