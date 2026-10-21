import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

type Appointment = {
  appointment_id: string; patient_name: string; provider_name: string; office_name: string;
  appointment_type: string; start: string; day: string; status: string;
};
type AuditEntry = { id: number; ts: string; tool: string; ok: boolean; error_code: string | null; args: object };
type Stats = { booked: number; insurance_verified: number; transferred_to_staff: number; refused_by_rules: number };
type State = { now: string; stats: Stats; appointments: Appointment[]; audit: AuditEntry[] };

const POLL_MS = 2000;

function Kpi({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`card kpi ${tone}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

function App() {
  const [state, setState] = useState<State | null>(null);
  const [online, setOnline] = useState(true);
  const seen = useRef<Set<string> | null>(null); // appointment ids already shown, to highlight new ones
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  useEffect(() => {
    const load = async () => {
      try {
        const data: State = await (await fetch("/api/state")).json();
        const ids = data.appointments.map((a) => a.appointment_id);
        if (seen.current) setFresh(new Set(ids.filter((id) => !seen.current!.has(id))));
        seen.current = new Set(ids);
        setState(data);
        setOnline(true);
      } catch {
        setOnline(false);
      }
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, []);

  if (!state) return <main className="muted">{online ? "Loading..." : "Cannot reach the EHR service on port 8000."}</main>;
  return (
    <main>
      <header>
        <div>
          <span className="eyebrow">AI Receptionist · synthetic data</span>
          <h1>Lakeview Family Clinic <span>front desk</span></h1>
          <div className="muted">Practice time {state.now}</div>
        </div>
        <div className="live"><span className={online ? "dot" : "dot off"} />{online ? "Live · refreshes every 2 s" : "EHR service offline"}</div>
      </header>

      <section className="kpis">
        <Kpi label="Appointments booked" value={state.stats.booked} tone="green" />
        <Kpi label="Insurance verified" value={state.stats.insurance_verified} tone="blue" />
        <Kpi label="Transferred to staff" value={state.stats.transferred_to_staff} tone="amber" />
        <Kpi label="Refused by rules" value={state.stats.refused_by_rules} tone="red" />
      </section>

      <section className="card">
        <h2>Appointments</h2>
        <table>
          <thead><tr><th>When</th><th>Patient</th><th>Provider</th><th>Office</th><th>Visit</th><th>Status</th></tr></thead>
          <tbody>
            {state.appointments.map((a) => (
              <tr key={a.appointment_id} className={fresh.has(a.appointment_id) ? "new" : ""}>
                <td>{a.day} {a.start.slice(5, 10)} {a.start.slice(11)}</td>
                <td>{a.patient_name}</td><td>{a.provider_name}</td><td>{a.office_name}</td>
                <td>{a.appointment_type}</td><td><span className={`pill ${a.status}`}>{a.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Audit trail</h2>
        <div className="muted" style={{ marginBottom: 8 }}>Every tool call the agent makes, including the ones the rules refused.</div>
        <table>
          <thead><tr><th>Time</th><th>Tool</th><th>Result</th><th>Arguments</th></tr></thead>
          <tbody>
            {state.audit.map((e) => (
              <tr key={e.id}>
                <td>{new Date(e.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
                <td>{e.tool}</td>
                <td><span className={e.ok ? "pill ok" : "pill err"}>{e.ok ? "ok" : e.error_code}</span></td>
                <td><code>{JSON.stringify(e.args)}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
