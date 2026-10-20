# Learning notes

My notes on every component: what it does, why it is built this way, and the trade-offs.
Written so I can explain each decision out loud.

---

## 1. MCP in one page

**What it is.** The Model Context Protocol is a standard way for an AI application to
connect to external systems. Instead of hard-coding tools inside the agent, a **server**
exposes capabilities and any MCP **client** can discover and use them. Messages are JSON-RPC.

**Roles.**
- **Host**: the app the user interacts with (Claude Desktop, an IDE, or my `text_agent.py`).
- **Client**: the connection the host opens to one server (`mcp.Client`).
- **Server**: exposes capabilities (`ehr_server/server.py`, the fake EHR).

**Transports.**
- **stdio**: the host launches the server as a subprocess and talks over stdin/stdout.
  Simple, local, no network. This is what `text_agent.py` uses. Consequence: the server must
  never `print()` to stdout, because stdout *is* the protocol channel. Logs go to stderr.
- **Streamable HTTP**: the server runs as a web service; many clients can connect remotely.
  This is what you would use in production (one EHR integration service, many agents).

**The three building blocks, and who controls each one:**

| Block | Controlled by | In this project |
|---|---|---|
| **Tools** | the **model** decides when to call them | `find_patient`, `search_slots`, `book_appointment`, `reschedule_appointment`, `cancel_appointment`, `verify_insurance`, `escalate_to_human` |
| **Resources** | the **application** decides what to load | `practice://directory`, `practice://rules`, `audit://recent`, `audit://appointments/{id}` (a *resource template*) |
| **Prompts** | the **user/app** picks a template | `receptionist_call`, the call script |

My agent uses all three: it fetches the `receptionist_call` prompt and the
`practice://directory` resource **once at startup** to build the system prompt, then the LLM
calls tools during the conversation. The directory is a resource, not a tool, because the
agent always needs it. Making the LLM "decide" to fetch it would waste a round trip.

**How a tool's schema is produced.** The Python type hints and `Field(description=...)` on each
tool function become the JSON Schema the LLM sees. `Literal[...]` becomes an `enum`, so the
protocol layer rejects `reason="bored"` before my code even runs. Good descriptions matter:
they are effectively part of the prompt.

**Tool annotations** (`read_only_hint`, `destructive_hint`) tell the host which tools are safe.
A host could auto-approve read-only tools and ask a human before destructive ones. They are
*hints*, not security: the server must still enforce everything itself.

**Two kinds of errors.**
- *Protocol errors* (`is_error=True`): malformed call, unknown tool, schema violation.
- *Domain errors*: the call was valid but the business said no (`PROVIDER_NOT_WORKING`).
  I return these as a normal result with `ok: false` and a stable `code`, `how_to_fix` and
  `alternatives`. The LLM can read the structure and recover in the same turn.

**SDK version note.** I used the official Python SDK **v2** (`mcp` 2.3). Changes from v1 that
tutorials still show: `FastMCP` was renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`),
and there is a new high-level `mcp.Client` that accepts a URL, `StdioServerParameters`, or a
server object (in-process, great for tests).

---

## 2. Why the business rules are deterministic code

**The problem (from Harrison, Kyron).** Protocols like "only book Dr. Johnson on Fridays at 2 p.m.
in Office A, not Office B" are trivial individually, but there are thousands, they differ per
customer, and they change constantly. If they live in the prompt, the LLM *usually* follows
them. "Usually" is not acceptable when the output is a real booking in a real EHR.

**My design:**

1. **Rule types are code** (`rules.py`): pure functions with no database, no clock and no LLM.
   Each one is unit-tested (`tests/test_rules.py`). Same input means same answer, every time.
2. **Rule instances are configuration** (`practice_rules.yaml`): which provider works when and
   where, who accepts new patients, which insurance plans, age limits, leave days. A customer's
   protocol change is a config edit, validated at startup (an unknown office id fails loudly).
   This is the "many customers per engineer" angle: onboarding a practice means writing config,
   not code.
3. **One check for everything.** `check_booking()` is used by `book_appointment`,
   `reschedule_appointment` **and** `search_slots`. Search can never offer a time that booking
   would refuse; there is a test that books the searched slots to prove it.
4. **Defence in depth against double booking:** rule check, then a `BEGIN IMMEDIATE` transaction
   (no other write can sneak in between check and insert), then a unique index in SQLite as the
   final guarantee.
5. **The prompt explains, the server enforces.** The prompt tells the LLM to verify identity and
   to only offer times from tools, but nothing breaks if it forgets: the server still says no.

**The flexibility vs. reliability trade-off** Harrison mentioned ("the more guardrails, the less
flexible"): I put guardrails on **actions** (what gets written to the EHR), not on the
**conversation**. The LLM can still handle weird callers, changes of mind and small talk freely.
And when a rule blocks something, the error carries **valid alternatives**, so the guardrail
redirects the conversation instead of dead-ending it.

**What it does not solve:** rules that are hard to express as data ("Dr. X prefers not to see
two new patients back to back unless..."). These need new rule *types* in code, plus tests. With
thousands of protocols you would want a rules DSL or a policy engine, and a way to test config
changes against recorded calls before deploying them.

---

## 3. Audit log

- Every tool call is recorded: arguments, structured result, error code, appointment id.
  **Rejected calls too.** "The agent tried Dr. Johnson on a Tuesday and was refused" is exactly
  what you want when reviewing a call or turning it into an eval case.
- **Append-only is enforced by the database**: SQLite triggers abort any `UPDATE`/`DELETE` on
  `audit_log` (tested). Appointments themselves are mutable state; the audit log is the
  immutable history.
- Auditing lives in the domain layer (`@audited` on `EHR` methods), not in the MCP adapter, so a
  future REST endpoint could not bypass it.
- Unexpected exceptions become a structured `INTERNAL_ERROR` (also audited) instead of crashing
  the call. The caller can still be escalated to a human.
- Caveat: the log stores patient data. In production it would need encryption, access control
  and retention rules (HIPAA).

---

## 4. Architecture of the server

```
practice_rules.yaml --> config.py (validated models)
                              |
MCP client --> server.py --> ehr.py (operations + @audited) --> rules.py (pure checks)
  (stdio)    (thin adapter)      |
                                 +--> SQLite: patients, appointments, escalations, audit_log
```

- **Practice structure in config, transactions in SQLite.** Providers and schedules change
  rarely and should be reviewable in a diff; appointments change every minute.
- **Frozen clock.** `EHR` takes a `now_fn`; tests and evals use a fixed "now" (or `EHR_NOW`), so
  "next Friday" means the same date every run.
- **Identity.** `find_patient` needs first name + last name + date of birth and returns the
  *same* error whether the name exists or not (no leaking who is a patient).
  Trade-off: write tools then trust `patient_id`. In production the server would tie the
  verified identity to the call session.
- **Office inference.** `office_id` is optional when the provider only works at one office that
  day. That means fewer arguments for a small LLM to get wrong.

---

## 5. The agent loop (text)

```
caller text -> LLM (with tool schemas) -> tool calls? --yes--> MCP call_tool -> result -> back to LLM
                                              \--no---> reply to caller
```

- **Hand-written loop** (`agent/core.py`), no agent framework, so every step is visible.
- **OpenAI-compatible API**: the same code runs on Groq (fast, free tier) or a local Ollama
  model by changing `.env`. MCP tool schemas are JSON Schema, which is what these APIs expect,
  so the conversion is a two-line wrapper.
- **Safety stops**: max 8 tool rounds per turn; invalid JSON arguments are returned to the model
  as a structured error instead of crashing; provider errors produce a polite "could you repeat that".
- **Timing per turn** (LLM vs tools) is printed. It becomes the latency report in Stage 3.
- **The greeting is fixed text**, not generated: zero latency, and always correct.
- **Testable without an LLM**: `tests/test_agent_loop.py` drives the loop with a scripted fake
  LLM and checks that the structured rule error actually reaches the model.

---

## 6. Voice pipeline

_To be written in Stage 2._

---

## 7. Questions I should be ready for

- *Why MCP instead of plain function calling?* The EHR integration becomes a reusable, separately
  deployable service with a standard interface: any agent (voice, text, a staff copilot, an eval
  harness) can use the same tools, and the rules live with the data, not with each agent.
- *Why not put the rules in the prompt?* Prompts are probabilistic and hard to test; code is
  deterministic, unit-tested and auditable. The prompt only shapes the conversation.
- *How would you scale to thousands of protocols?* Rule types as tested code, instances as
  versioned per-customer config, config validated at load, and replaying recorded calls against
  a config change before shipping it.
- *What if two callers book the same slot at once?* Transaction with an immediate write lock plus
  a unique index; the loser gets `SLOT_TAKEN` with alternatives.
