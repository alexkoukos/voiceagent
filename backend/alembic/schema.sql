-- The schema at revision 0033, from pg_dump. Later changes are new migrations.
CREATE TYPE appointmentstatus AS ENUM (
    'booked',
    'cancelled'
);
CREATE TYPE callstatus AS ENUM (
    'pending',
    'dialing',
    'active',
    'completed',
    'failed',
    'queued',
    'cancelled'
);
CREATE TYPE transcriptrole AS ENUM (
    'agent',
    'friend'
);
CREATE TABLE admin_links (
    token_hash character varying NOT NULL,
    practice_id character varying NOT NULL,
    staff_id character varying,
    expires_at timestamp without time zone NOT NULL,
    revoked boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    last_used_at timestamp without time zone
);
CREATE TABLE admin_requests (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    channel character varying NOT NULL,
    sender character varying NOT NULL,
    staff_id character varying,
    call_id character varying,
    text text DEFAULT ''::text NOT NULL,
    parsed json DEFAULT '{}'::json NOT NULL,
    readback text DEFAULT ''::text NOT NULL,
    status character varying DEFAULT 'pending'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    decided_at timestamp without time zone
);
CREATE TABLE alerts (
    id character varying NOT NULL,
    practice_id character varying,
    kind character varying NOT NULL,
    call_id character varying,
    subject character varying DEFAULT ''::character varying NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    dedupe_key character varying,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    acked_at timestamp without time zone,
    escalated_at timestamp without time zone
);
CREATE TABLE appointments (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    call_id character varying,
    customer_name character varying NOT NULL,
    customer_phone character varying,
    service_id character varying NOT NULL,
    service_name character varying NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    status appointmentstatus NOT NULL,
    gcal_event_id character varying,
    idempotency_key character varying,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    staff_id character varying,
    customer_id character varying,
    source character varying DEFAULT 'agent'::character varying NOT NULL,
    reminder_status character varying DEFAULT 'none'::character varying NOT NULL,
    updated_at timestamp without time zone
);
CREATE TABLE billing_accounts (
    practice_id character varying NOT NULL,
    pilot_started_on date NOT NULL,
    monthly_fee numeric(10,2) NOT NULL
);
CREATE TABLE billing_drafts (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    bookings integer NOT NULL,
    threshold integer NOT NULL,
    amount numeric(10,2) NOT NULL,
    status character varying NOT NULL,
    call_ids json NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE TABLE calendar_connections (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    staff_id character varying,
    google_email text DEFAULT ''::text NOT NULL,
    calendar_id character varying NOT NULL,
    refresh_token text NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE calendar_feeds (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    staff_id character varying,
    name character varying NOT NULL,
    url text NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE TABLE call_telemetry_events (
    id bigint NOT NULL,
    call_id character varying NOT NULL,
    session_id character varying(36) NOT NULL,
    sequence integer NOT NULL,
    payload json NOT NULL,
    received_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE call_telemetry_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE call_telemetry_events_id_seq OWNED BY call_telemetry_events.id;
CREATE TABLE calls (
    id character varying NOT NULL,
    voice character varying NOT NULL,
    max_duration_seconds integer NOT NULL,
    status callstatus NOT NULL,
    recording_url character varying,
    duration_seconds integer,
    created_at timestamp without time zone NOT NULL,
    started_at timestamp without time zone,
    ended_at timestamp without time zone,
    delete_requested boolean DEFAULT false NOT NULL,
    end_reason character varying,
    language character varying,
    practice_id character varying,
    direction character varying DEFAULT 'outbound'::character varying NOT NULL,
    caller_number character varying,
    use_case character varying,
    outcome character varying,
    appointment_id character varying,
    summary text,
    flags json DEFAULT '[]'::json NOT NULL,
    cost_estimate double precision,
    latency_ms_median integer,
    hours_state character varying,
    purpose character varying,
    review json,
    finalized boolean DEFAULT false NOT NULL,
    customer_id character varying,
    cost_breakdown json,
    demo_dashboard_id character varying,
    clock_start timestamp without time zone,
    flow json
);
CREATE TABLE config_versions (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    status character varying DEFAULT 'published'::character varying NOT NULL,
    source character varying DEFAULT 'app'::character varying NOT NULL,
    author character varying DEFAULT ''::character varying NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    changes json DEFAULT '{}'::json NOT NULL,
    snapshot json,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    decided_at timestamp without time zone
);
CREATE TABLE customers (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    phone character varying NOT NULL,
    name character varying DEFAULT ''::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE data_requests (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    phone_hash character varying NOT NULL,
    kind character varying NOT NULL,
    counts json DEFAULT '{}'::json NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE demo_dashboards (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    token_hash character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone,
    revoked_at timestamp without time zone
);
CREATE TABLE demo_leads (
    id character varying NOT NULL,
    name text NOT NULL,
    business_name text NOT NULL,
    business_type character varying(40) NOT NULL,
    contact_method character varying(10) NOT NULL,
    contact_detail text NOT NULL,
    website text NOT NULL,
    source_hash character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE TABLE devices (
    token character varying NOT NULL,
    practice_id character varying,
    staff_id character varying,
    environment character varying DEFAULT 'sandbox'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE handoffs (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    call_id character varying NOT NULL,
    staff_id character varying,
    room_name character varying NOT NULL,
    mode character varying DEFAULT 'app'::character varying NOT NULL,
    status character varying DEFAULT 'ringing'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    resolved_at timestamp without time zone
);
CREATE TABLE health_checks (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    kind character varying NOT NULL,
    number character varying,
    success boolean NOT NULL,
    detail character varying NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE TABLE imports (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    version_id character varying,
    source character varying NOT NULL,
    source_ref text NOT NULL,
    raw_payload text NOT NULL,
    extracted json NOT NULL,
    confidence json NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE TABLE messages (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    call_id character varying,
    staff_id character varying,
    caller_name character varying DEFAULT ''::character varying NOT NULL,
    callback_number character varying,
    reason text DEFAULT ''::text NOT NULL,
    best_time character varying DEFAULT ''::character varying NOT NULL,
    urgent boolean DEFAULT false NOT NULL,
    status character varying DEFAULT 'new'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE notifications (
    id character varying NOT NULL,
    practice_id character varying,
    call_id character varying,
    kind character varying NOT NULL,
    channel character varying NOT NULL,
    recipient character varying NOT NULL,
    subject character varying DEFAULT ''::character varying NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    data json DEFAULT '{}'::json NOT NULL,
    status character varying DEFAULT 'pending'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp without time zone DEFAULT now() NOT NULL,
    last_error text,
    dedupe_key character varying,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    sent_at timestamp without time zone
);
CREATE TABLE practices (
    id character varying NOT NULL,
    name character varying NOT NULL,
    slug character varying,
    timezone character varying DEFAULT 'Europe/Athens'::character varying NOT NULL,
    language character varying DEFAULT 'el'::character varying NOT NULL,
    voice character varying DEFAULT 'Algieba'::character varying NOT NULL,
    greeting text DEFAULT ''::text NOT NULL,
    phone_numbers json DEFAULT '[]'::json NOT NULL,
    hours json DEFAULT '{}'::json NOT NULL,
    services json DEFAULT '[]'::json NOT NULL,
    rules json DEFAULT '{}'::json NOT NULL,
    knowledge_base json DEFAULT '{}'::json NOT NULL,
    calendar_id character varying,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    vertical character varying DEFAULT ''::character varying NOT NULL,
    routing_rules json DEFAULT '{}'::json NOT NULL,
    departments json DEFAULT '[]'::json NOT NULL,
    notifications json DEFAULT '{}'::json NOT NULL,
    reminders json DEFAULT '{}'::json NOT NULL,
    outbound_number character varying,
    max_concurrent_calls integer DEFAULT 2 NOT NULL,
    retention_recordings_days integer DEFAULT 30 NOT NULL,
    retention_transcripts_days integer DEFAULT 90 NOT NULL,
    avg_booking_value double precision DEFAULT '0'::double precision NOT NULL,
    guarantee_threshold integer DEFAULT 10 NOT NULL,
    monthly_cost_cap_eur double precision,
    blocked_numbers json DEFAULT '[]'::json NOT NULL,
    offboarded_at timestamp without time zone,
    admin_pin_hash character varying,
    publish_frozen boolean DEFAULT false NOT NULL,
    google_place_id character varying,
    google_refreshed_at timestamp without time zone,
    call_routing json DEFAULT '{}'::json NOT NULL
);
CREATE TABLE recording_deletions (
    key character varying NOT NULL,
    call_id character varying,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp without time zone DEFAULT now() NOT NULL,
    last_error text,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE routing_events (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    call_id character varying NOT NULL,
    kind character varying NOT NULL,
    value character varying DEFAULT ''::character varying NOT NULL,
    rule character varying DEFAULT ''::character varying NOT NULL,
    path character varying DEFAULT ''::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE staff (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    name character varying NOT NULL,
    role character varying DEFAULT 'staff'::character varying NOT NULL,
    aliases json DEFAULT '[]'::json NOT NULL,
    service_ids json DEFAULT '[]'::json NOT NULL,
    hours json DEFAULT '{}'::json NOT NULL,
    calendar_id character varying,
    phone character varying,
    email character varying,
    bookable boolean DEFAULT true NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE tenant_api_keys (
    token_hash character varying NOT NULL,
    practice_id character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    expires_at timestamp without time zone,
    revoked_at timestamp without time zone
);
CREATE TABLE transcript_entries (
    id character varying NOT NULL,
    call_id character varying NOT NULL,
    role transcriptrole NOT NULL,
    text text NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE TABLE waitlist (
    id character varying NOT NULL,
    practice_id character varying NOT NULL,
    customer_name character varying DEFAULT ''::character varying NOT NULL,
    phone character varying NOT NULL,
    service_id character varying NOT NULL,
    staff_id character varying,
    date_from date NOT NULL,
    date_to date NOT NULL,
    part_of_day character varying,
    status character varying DEFAULT 'waiting'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY call_telemetry_events ALTER COLUMN id SET DEFAULT nextval('call_telemetry_events_id_seq'::regclass);
ALTER TABLE ONLY admin_links
    ADD CONSTRAINT admin_links_pkey PRIMARY KEY (token_hash);
ALTER TABLE ONLY admin_requests
    ADD CONSTRAINT admin_requests_pkey PRIMARY KEY (id);
ALTER TABLE ONLY alerts
    ADD CONSTRAINT alerts_dedupe_key_key UNIQUE (dedupe_key);
ALTER TABLE ONLY alerts
    ADD CONSTRAINT alerts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT appointments_idempotency_key_key UNIQUE (idempotency_key);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT appointments_pkey PRIMARY KEY (id);
ALTER TABLE ONLY billing_accounts
    ADD CONSTRAINT billing_accounts_pkey PRIMARY KEY (practice_id);
ALTER TABLE ONLY billing_drafts
    ADD CONSTRAINT billing_drafts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY calendar_connections
    ADD CONSTRAINT calendar_connections_pkey PRIMARY KEY (id);
ALTER TABLE ONLY calendar_feeds
    ADD CONSTRAINT calendar_feeds_pkey PRIMARY KEY (id);
ALTER TABLE ONLY call_telemetry_events
    ADD CONSTRAINT call_telemetry_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY calls
    ADD CONSTRAINT calls_pkey PRIMARY KEY (id);
ALTER TABLE ONLY config_versions
    ADD CONSTRAINT config_versions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (id);
ALTER TABLE ONLY customers
    ADD CONSTRAINT customers_practice_id_phone_key UNIQUE (practice_id, phone);
ALTER TABLE ONLY data_requests
    ADD CONSTRAINT data_requests_pkey PRIMARY KEY (id);
ALTER TABLE ONLY demo_dashboards
    ADD CONSTRAINT demo_dashboards_pkey PRIMARY KEY (id);
ALTER TABLE ONLY demo_dashboards
    ADD CONSTRAINT demo_dashboards_token_hash_key UNIQUE (token_hash);
ALTER TABLE ONLY demo_leads
    ADD CONSTRAINT demo_leads_pkey PRIMARY KEY (id);
ALTER TABLE ONLY devices
    ADD CONSTRAINT devices_pkey PRIMARY KEY (token);
ALTER TABLE ONLY handoffs
    ADD CONSTRAINT handoffs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY health_checks
    ADD CONSTRAINT health_checks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY imports
    ADD CONSTRAINT imports_pkey PRIMARY KEY (id);
ALTER TABLE ONLY messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY notifications
    ADD CONSTRAINT notifications_dedupe_key_key UNIQUE (dedupe_key);
ALTER TABLE ONLY notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);
ALTER TABLE ONLY practices
    ADD CONSTRAINT practices_pkey PRIMARY KEY (id);
ALTER TABLE ONLY practices
    ADD CONSTRAINT practices_slug_key UNIQUE (slug);
ALTER TABLE ONLY recording_deletions
    ADD CONSTRAINT recording_deletions_pkey PRIMARY KEY (key);
ALTER TABLE ONLY routing_events
    ADD CONSTRAINT routing_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY staff
    ADD CONSTRAINT staff_pkey PRIMARY KEY (id);
ALTER TABLE ONLY tenant_api_keys
    ADD CONSTRAINT tenant_api_keys_pkey PRIMARY KEY (token_hash);
ALTER TABLE ONLY transcript_entries
    ADD CONSTRAINT transcript_entries_pkey PRIMARY KEY (id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT uq_appointments_tenant_id UNIQUE (practice_id, id);
ALTER TABLE ONLY billing_drafts
    ADD CONSTRAINT uq_billing_period UNIQUE (practice_id, period_start);
ALTER TABLE ONLY call_telemetry_events
    ADD CONSTRAINT uq_call_telemetry_sequence UNIQUE (call_id, session_id, sequence);
ALTER TABLE ONLY calls
    ADD CONSTRAINT uq_calls_tenant_id UNIQUE (practice_id, id);
ALTER TABLE ONLY config_versions
    ADD CONSTRAINT uq_config_versions_tenant_id UNIQUE (practice_id, id);
ALTER TABLE ONLY customers
    ADD CONSTRAINT uq_customers_tenant_id UNIQUE (practice_id, id);
ALTER TABLE ONLY staff
    ADD CONSTRAINT uq_staff_tenant_id UNIQUE (practice_id, id);
ALTER TABLE ONLY waitlist
    ADD CONSTRAINT waitlist_pkey PRIMARY KEY (id);
CREATE INDEX ix_admin_links_practice_id ON admin_links USING btree (practice_id);
CREATE INDEX ix_admin_requests_practice_id ON admin_requests USING btree (practice_id);
CREATE INDEX ix_admin_requests_sender ON admin_requests USING btree (sender);
CREATE INDEX ix_alerts_practice_id ON alerts USING btree (practice_id);
CREATE INDEX ix_appointments_practice_id ON appointments USING btree (practice_id);
CREATE INDEX ix_billing_drafts_practice_id ON billing_drafts USING btree (practice_id);
CREATE INDEX ix_calendar_connections_calendar_id ON calendar_connections USING btree (calendar_id);
CREATE INDEX ix_calendar_connections_practice_id ON calendar_connections USING btree (practice_id);
CREATE INDEX ix_calendar_feeds_practice_id ON calendar_feeds USING btree (practice_id);
CREATE INDEX ix_call_telemetry_call_cursor ON call_telemetry_events USING btree (call_id, id);
CREATE INDEX ix_calls_demo_dashboard_id ON calls USING btree (demo_dashboard_id);
CREATE INDEX ix_config_versions_practice_id ON config_versions USING btree (practice_id);
CREATE INDEX ix_customers_practice_id ON customers USING btree (practice_id);
CREATE INDEX ix_data_requests_practice_id ON data_requests USING btree (practice_id);
CREATE INDEX ix_demo_leads_created_at ON demo_leads USING btree (created_at);
CREATE INDEX ix_demo_leads_source_hash ON demo_leads USING btree (source_hash);
CREATE INDEX ix_handoffs_practice_id ON handoffs USING btree (practice_id);
CREATE INDEX ix_health_checks_created_at ON health_checks USING btree (created_at);
CREATE INDEX ix_health_checks_practice_id ON health_checks USING btree (practice_id);
CREATE INDEX ix_imports_practice_id ON imports USING btree (practice_id);
CREATE INDEX ix_messages_practice_id ON messages USING btree (practice_id);
CREATE INDEX ix_notifications_practice_id ON notifications USING btree (practice_id);
CREATE INDEX ix_notifications_status ON notifications USING btree (status);
CREATE INDEX ix_routing_events_call_id ON routing_events USING btree (call_id);
CREATE INDEX ix_routing_events_practice_id ON routing_events USING btree (practice_id);
CREATE INDEX ix_staff_practice_id ON staff USING btree (practice_id);
CREATE INDEX ix_tenant_api_keys_practice_id ON tenant_api_keys USING btree (practice_id);
CREATE INDEX ix_waitlist_practice_id ON waitlist USING btree (practice_id);
ALTER TABLE ONLY admin_links
    ADD CONSTRAINT admin_links_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY admin_links
    ADD CONSTRAINT admin_links_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY admin_requests
    ADD CONSTRAINT admin_requests_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY admin_requests
    ADD CONSTRAINT admin_requests_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY alerts
    ADD CONSTRAINT alerts_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT appointments_call_id_fkey FOREIGN KEY (call_id) REFERENCES calls(id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT appointments_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES customers(id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT appointments_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT appointments_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY billing_accounts
    ADD CONSTRAINT billing_accounts_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY billing_drafts
    ADD CONSTRAINT billing_drafts_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY calendar_connections
    ADD CONSTRAINT calendar_connections_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY calendar_connections
    ADD CONSTRAINT calendar_connections_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY calendar_feeds
    ADD CONSTRAINT calendar_feeds_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY calendar_feeds
    ADD CONSTRAINT calendar_feeds_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY call_telemetry_events
    ADD CONSTRAINT call_telemetry_events_call_id_fkey FOREIGN KEY (call_id) REFERENCES calls(id) ON DELETE CASCADE;
ALTER TABLE ONLY calls
    ADD CONSTRAINT calls_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES customers(id);
ALTER TABLE ONLY calls
    ADD CONSTRAINT calls_demo_dashboard_id_fkey FOREIGN KEY (demo_dashboard_id) REFERENCES demo_dashboards(id) ON DELETE SET NULL;
ALTER TABLE ONLY calls
    ADD CONSTRAINT calls_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY config_versions
    ADD CONSTRAINT config_versions_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY customers
    ADD CONSTRAINT customers_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY data_requests
    ADD CONSTRAINT data_requests_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY demo_dashboards
    ADD CONSTRAINT demo_dashboards_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id) ON DELETE CASCADE;
ALTER TABLE ONLY devices
    ADD CONSTRAINT devices_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY devices
    ADD CONSTRAINT devices_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY admin_links
    ADD CONSTRAINT fk_admin_links_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY admin_requests
    ADD CONSTRAINT fk_admin_requests_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT fk_appointments_call_id_tenant FOREIGN KEY (practice_id, call_id) REFERENCES calls(practice_id, id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT fk_appointments_customer_id_tenant FOREIGN KEY (practice_id, customer_id) REFERENCES customers(practice_id, id);
ALTER TABLE ONLY appointments
    ADD CONSTRAINT fk_appointments_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY calendar_connections
    ADD CONSTRAINT fk_calendar_connections_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY calendar_feeds
    ADD CONSTRAINT fk_calendar_feeds_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY calls
    ADD CONSTRAINT fk_calls_appointment_id_tenant FOREIGN KEY (practice_id, appointment_id) REFERENCES appointments(practice_id, id);
ALTER TABLE ONLY calls
    ADD CONSTRAINT fk_calls_customer_id_tenant FOREIGN KEY (practice_id, customer_id) REFERENCES customers(practice_id, id);
ALTER TABLE ONLY devices
    ADD CONSTRAINT fk_devices_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY handoffs
    ADD CONSTRAINT fk_handoffs_call_id_tenant FOREIGN KEY (practice_id, call_id) REFERENCES calls(practice_id, id);
ALTER TABLE ONLY handoffs
    ADD CONSTRAINT fk_handoffs_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY imports
    ADD CONSTRAINT fk_imports_version_id_tenant FOREIGN KEY (practice_id, version_id) REFERENCES config_versions(practice_id, id);
ALTER TABLE ONLY messages
    ADD CONSTRAINT fk_messages_call_id_tenant FOREIGN KEY (practice_id, call_id) REFERENCES calls(practice_id, id);
ALTER TABLE ONLY messages
    ADD CONSTRAINT fk_messages_staff_id_tenant FOREIGN KEY (practice_id, staff_id) REFERENCES staff(practice_id, id);
ALTER TABLE ONLY routing_events
    ADD CONSTRAINT fk_routing_events_call_id_tenant FOREIGN KEY (practice_id, call_id) REFERENCES calls(practice_id, id);
ALTER TABLE ONLY handoffs
    ADD CONSTRAINT handoffs_call_id_fkey FOREIGN KEY (call_id) REFERENCES calls(id);
ALTER TABLE ONLY handoffs
    ADD CONSTRAINT handoffs_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY handoffs
    ADD CONSTRAINT handoffs_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY health_checks
    ADD CONSTRAINT health_checks_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY imports
    ADD CONSTRAINT imports_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY imports
    ADD CONSTRAINT imports_version_id_fkey FOREIGN KEY (version_id) REFERENCES config_versions(id);
ALTER TABLE ONLY messages
    ADD CONSTRAINT messages_call_id_fkey FOREIGN KEY (call_id) REFERENCES calls(id);
ALTER TABLE ONLY messages
    ADD CONSTRAINT messages_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY messages
    ADD CONSTRAINT messages_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES staff(id);
ALTER TABLE ONLY notifications
    ADD CONSTRAINT notifications_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY recording_deletions
    ADD CONSTRAINT recording_deletions_call_id_fkey FOREIGN KEY (call_id) REFERENCES calls(id);
ALTER TABLE ONLY routing_events
    ADD CONSTRAINT routing_events_call_id_fkey FOREIGN KEY (call_id) REFERENCES calls(id);
ALTER TABLE ONLY routing_events
    ADD CONSTRAINT routing_events_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY staff
    ADD CONSTRAINT staff_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY tenant_api_keys
    ADD CONSTRAINT tenant_api_keys_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
ALTER TABLE ONLY transcript_entries
    ADD CONSTRAINT transcript_entries_call_id_fkey FOREIGN KEY (call_id) REFERENCES calls(id);
ALTER TABLE ONLY waitlist
    ADD CONSTRAINT waitlist_practice_id_fkey FOREIGN KEY (practice_id) REFERENCES practices(id);
