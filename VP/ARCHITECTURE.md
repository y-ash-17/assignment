# Care Concierge: technical architecture and delivery plan

This is a proposed target architecture based on the repository, not a claim that the system is complete. Confirm the assignment brief, messaging provider, clinical workflow and acceptance criteria before finalizing decisions. The repository is a foundation and is not yet a working end-to-end application.

## 1. Purpose and scope

Provide a multilingual residential healthcare concierge for clinic information, doctor and lab bookings, symptom logging and emergency escalation. The system should support English, Hindi and Hinglish, protect patient data, prevent duplicate actions and double bookings, and respond safely when dependencies fail. It should not diagnose, prescribe or replace emergency services. A production rollout needs clinical and operational review.

## 2. Proposed technology stack

| Layer | Proposed technology | Role and status |
| --- | --- | --- |
| Language and runtime | Python 3.12+ | Existing project requirement. |
| API and validation | FastAPI, Pydantic v2, Uvicorn | Declared dependencies; the application entry point and webhook are missing. |
| Messaging | WhatsApp Business provider webhook and outbound API, behind a provider adapter | Proposed; select provider and verify its signature, delivery and retry contract against the assignment. Fixtures exist; integration does not. |
| Conversation orchestration | LangGraph and LangChain, with explicit state and bounded tool calls | Dependencies and tool wrappers exist; conversation graph and persistent checkpoint integration remain. Simple deterministic routing is acceptable where an LLM is unnecessary. |
| AI model | Configurable hosted chat model through a provider adapter, structured outputs, timeouts and replay fixtures | Factory and safety classifier exist. Provider packages, model selection, keys, costs and behavior must be verified. Do not send patient data to a provider without approved terms and controls. |
| Retrieval | Sentence Transformers `intfloat/multilingual-e5-small`, PostgreSQL pgvector | Embedding and search code exist; ingestion, evaluation and answer grounding remain. Model dimension must match the 384-dimensional schema. |
| Persistence | PostgreSQL 16 with pgvector, SQLAlchemy async, psycopg, Alembic | Models, initial migration and seed loader exist. PostgreSQL enforces booking uniqueness and transactions. |
| Background delivery | PostgreSQL transactional outbox plus a worker | Proposed for reliable outbound messages and escalation notifications; no worker/outbox currently exists. Add a separate queue only if scale warrants it. |
| Packaging and local development | Docker, Docker Compose, environment-based configuration | Files exist, but Compose references missing modules and cannot currently start the full app. |
| Quality and operations | pytest, pytest-asyncio, Ruff, CI, structured logs and monitoring | Some tests and logging exist; CI, full integration coverage and operational monitoring remain. |

## 3. System and trust boundaries

```text
Patient / WhatsApp
       |
       v
Messaging provider ---- signed webhook ----> FastAPI ingress
                                                  |
                                  verify, normalize, deduplicate
                                                  |
                                  resolve sender -> patient + society
                                                  |
                                       safety classification
                                /                 |                 \
                       emergency          medical advice         ordinary
                           |                 |                     |
                 immediate response    safe refusal /       conversation controller
                 + escalation          appropriate help      (bounded tool calls)
                           |                                       |
                           +----------------+----------------------+
                                            |
                 scoped domain tools + document retrieval
                                            |
                  PostgreSQL / pgvector + transactional outbox
                                            |
                         worker -> provider / escalation staff
```

Ingress is the trust boundary for provider authenticity; patient resolution and authorization are separate from signature verification. Only server-controlled context supplies patient and society IDs to tools. Model output and retrieved text are untrusted: validate tool arguments and do not treat document text as system instructions. Only approved staff can view or acknowledge escalations.

## 4. Component responsibilities

1. **Webhook ingress:** verify the provider signature against the raw request body; handle verification challenges, text, unsupported media and delivery status events according to the selected provider. Reject invalid signatures. Normalize provider IDs and timestamps, and persist the inbound event with a unique provider message ID before acknowledging it. Never process delivery status as a patient message.
2. **Identity and tenant context:** map the verified sender to a patient and society using server-side records. Handle unknown or ambiguous senders without exposing patient details. Sender identity alone must meet the assignment's authentication requirements; use stronger verification where necessary.
3. **Safety gate:** classify each incoming patient message before normal processing. Emergency signals take priority; send an appropriate immediate safety response and create a durable escalation. On classification timeout or failure, block ordinary tool execution and return the approved fallback. Medical advice requests receive the approved safe response. Validate emergency coverage and templates with clinical stakeholders.
4. **Conversation controller:** maintain minimal state per conversation, detect intent and required information, ask for missing booking details, and route to approved tools. Enforce timeouts, a tool-call cap and structured input validation. Do not allow model-generated identity, access decisions or unsupported clinical advice.
5. **Domain tools:** book and list services, log symptoms and raise escalations using server-injected identity. Validate inputs, use transactions and return stable structured results. Booking must use the society timezone, enforce lead time and availability, prevent active slot collisions, and offer valid alternatives. Make idempotency safe under concurrent delivery and define how reused IDs with changed payloads are handled.
6. **Retrieval:** ingest approved documents into versioned chunks, store normalized embeddings, filter by current version and authorized society, retrieve relevant passages, and return citations. Apply a tested relevance threshold and admit when evidence is insufficient. Exclude internal-only material. Do not rely on a model to enforce access filters.
7. **Outbound and escalation worker:** deliver persisted responses and staff alerts using an outbox and retry policy. Record provider delivery IDs and status; use provider idempotency where supported. Escalations need a named on-call owner, acknowledgement, deadline monitoring and a tested fallback when notification fails. Persisting a row alone is not notification.

## 5. Main flows and failure handling

- **Inbound and duplicates:** signature check -> normalize -> unique event insert -> resolve patient -> safety -> process once -> persist result/outbox in a transaction -> acknowledge. Redelivery returns or reuses the recorded result; concurrent duplicates must not create extra actions. Design processing recovery for events left in progress after a crash.
- **Booking:** confirm service, subtype and local time with the patient as required -> validate eligibility and grid -> attempt a database insert -> return confirmation or eligible alternatives. The database unique index is the final protection against concurrent bookings. Try other eligible clinicians when appropriate and avoid leaking another patient's details.
- **Clinic question:** filter current, authorized documents -> retrieve and assess evidence -> produce an answer grounded in cited passages, or say the information is unavailable and offer an appropriate contact.
- **Emergency:** prioritize immediate approved safety guidance and contact information -> persist escalation and notification work -> alert staff and track acknowledgement/deadline. Define and test fallback behavior for database or notification failure; do not claim an alert succeeded without confirmation.
- **Dependency failure:** use bounded timeouts, retry only transient failures with backoff, avoid duplicate side effects, and provide a safe response. A safety failure must never silently become a normal conversation. Provider delivery failure must remain visible for retry or staff intervention.

## 6. Data and security design

Existing tables cover societies, patients, clinicians, service requests, symptom logs, escalations, documents and chunks. Proposed additions include inbound event/deduplication records, conversation state/checkpoints, outbox delivery records and auditable escalation acknowledgement. Define unique keys, retention, indexes and migrations for these before implementation. Store timestamps as timezone-aware values and render them in the society timezone.

Apply least-privilege database and service credentials, TLS in transit, encrypted storage and backups where available, secret management outside source control, access controls and audit trails. Minimize patient details in logs, prompts, caches and fixtures; avoid real patient data in test recordings. Define retention and deletion policies with the operator. Tenant filters must be enforced by the application and tested, including direct queries and background jobs. These are design requirements, not verified compliance claims.

## 7. Deployment and operations

Local development uses a Python 3.12 environment and PostgreSQL with pgvector, or Docker Compose once the missing API and ingestion modules are implemented. Run Alembic migrations deliberately before serving traffic; load seed data only for appropriate environments. Production should use separately managed configuration and secrets, a managed or backed-up database, an API process and a worker, health/readiness checks, deployment rollback and tested database restore. Avoid destructive seed or migration operations on real data.

Monitor webhook acceptance and signature failures, processing latency, failed deliveries, safety fallback rate, booking conflicts, retrieval no-answer rate, escalation acknowledgement and overdue alerts. Protect metrics and logs from exposing patient information. Set actual thresholds and an on-call runbook with the operator.

## 8. Testing and acceptance

- Unit-test slot calculations, validation, classification fallback and routing with deterministic fixtures.
- Run migration and tool integration tests against disposable PostgreSQL with pgvector; verify concurrent booking, duplicate messages, tenant isolation and rollback.
- Add webhook-to-response tests for booking, clinic questions, emergency messages, multilingual inputs, status events, unknown senders and unsupported media.
- Exercise provider timeout, database outage, retries, worker restart, duplicate/out-of-order deliveries, missing evidence and failed escalation notification.
- Evaluate multilingual retrieval and safety with an agreed labeled dataset and clinical review. Acceptance requires an authorized patient to complete the flows safely, no cross-tenant disclosure, no duplicate side effects, and observable escalation delivery; agree numerical thresholds with stakeholders.
- CI should install locked dependencies, lint, run tests against a disposable database and build the container. Keep evaluation fixtures synthetic or approved.

## 9. Current implementation and remaining work

**Present:** database models and migration, seed data, slot calculations, scoped repository, five domain functions, booking uniqueness and request IDs, safety classifier and regex fallback, response templates, LLM cache, embedding wrapper, webhook fixtures and several tests. Booking replay after a slot passes, overlapping slot deduplication and non-positive slot validation were added in this change.

**Still required, in order:**

1. Restore reproducible setup and run the existing PostgreSQL-backed suite. Compose references missing `app.retrieval.ingest` and `app.api.main` modules.
2. Implement ingestion, document versioning and scoped retrieval evaluation.
3. Build authenticated webhook ingress, sender resolution, event deduplication and delivery handling.
4. Connect the safety gate, conversation controller, state and domain tools into an end-to-end flow.
5. Complete booking edge cases, including alternate clinicians, request-ID payload consistency and concurrency; audit authorization and all direct queries.
6. Implement outbox delivery, staff notification, escalation acknowledgement and SLA monitoring.
7. Add end-to-end tests, CI, operations documentation and clinical/security review.

A preliminary estimate is **30–40% complete and 60–70% remaining**. This is an architectural judgment, not a measured project metric: the assignment brief is unavailable and the complete test suite has not been verified. Re-estimate against agreed acceptance criteria after the first working end-to-end flow.
