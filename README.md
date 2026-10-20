# Voice receptionist on MCP (learning project)

A minimal AI receptionist that books medical appointments in a **fake EHR exposed as an MCP
server**, inspired by AI front-desk products for medical practices. Business rules (which
provider works when and where, insurance, age limits...) are **enforced by deterministic server
code**, not by the LLM prompt.

> All data is synthetic. This is a learning project, not a medical product.

## Architecture

```
                  text or voice
                         |
                 agent/core.py  --(OpenAI-compatible API)-->  LLM (Groq or local Ollama)
                         |
                   MCP over stdio
                         |
               ehr_server/server.py   tools . resources . prompt
                         |
               ehr_server/ehr.py      operations + append-only audit
                  |             |
          rules.py (pure)    SQLite (patients, appointments, audit_log)
                  |
       practice_rules.yaml (per-practice protocol config)
```

- **Tools:** `find_patient`, `search_slots`, `book_appointment`, `reschedule_appointment`,
  `cancel_appointment`, `verify_insurance` (simulated), `escalate_to_human`
- **Resources:** `practice://directory`, `practice://rules`, `audit://recent`, `audit://appointments/{id}`
- **Prompt:** `receptionist_call`

Every failure returns a structured error: `code`, `how_to_fix` and valid `alternatives`, so the
agent can recover. See [LEARNING_NOTES.md](LEARNING_NOTES.md) for the reasoning behind each decision.

## Run it (Windows, Python 3.11)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"

# create a .env file with: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL (Groq), plus LIVEKIT_URL,
# LIVEKIT_API_KEY, LIVEKIT_API_SECRET and TTS_PROVIDER for the voice agent

pytest                      # rules, operations, audit log, MCP protocol, agent loop
python -m ehr_server.seed   # (optional) rebuild the synthetic database
python -m agent.text_agent  # chat with the receptionist
```

Try: *"Hi, I'm James Carter, born November 2nd 1972. I'd like to see Dr. Johnson next Tuesday."*
Dr. Johnson only works Fridays at 2 p.m. at Office A, so the server refuses and offers the next valid slot.

### Synthetic patients for testing

| Name | DOB | Notes |
|---|---|---|
| Maria Lopez | 1985-03-14 | BlueCross, has an upcoming visit with Dr. Patel |
| James Carter | 1972-11-02 | Aetna, holds Dr. Johnson's slot this Friday |
| Lily Chen | 2015-06-21 | Child, sees the pediatrician |
| Robert Smith | 1990-01-30 | Insurance **inactive** |
| Aisha Khan | 1999-08-09 | **New** patient, Cigna (out of network for Dr. Johnson) |
| Daniel Brooks | 1965-05-05 | Self-pay |
| John Miller | 1980-04-04 / 1955-09-19 | Two patients with the same name |

## Limitations (honest)

- **Not production-ready.** No real telephony, no authentication beyond name and date of birth,
  no real EHR integration, simulated insurance checks.
- **Synthetic data only.** The audit log stores patient data in plain SQLite; real use would need
  encryption, access control, retention rules, BAAs with every vendor (LLM, speech) and HIPAA review.
- Single practice, naive local times (no timezone handling), no concurrency testing beyond
  SQLite's write lock.