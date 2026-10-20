import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

type Appointment = {
  appointment_id: string; patient_name: string; provider_name: string; office_name: string;
  appointment_type: string; start: string; day: string; status: string;
};
type AuditEntry = { id: number; ts: string; tool: string; ok: boolean; error_code: string | null; args: object };
type State = { now: string; appointments: Appointment[]; audit: AuditEntry[] };

const POLL_MS = 2000;

function App() {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
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
        setError("");
      } catch {
        setError("Cannot reach the EHR service. Is it running on port 8000?");
      }
    };
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, []);

  if (!state) return <main>{error || "Loading..."}</main>;
  return (
    <main>
      <h1>Lakeside Family Clinic: front desk</h1>
      <div className="muted">Practice time {state.now} · refreshes every 2 s · synthetic data {error && `· ${error}`}</div>

      <h2>Appointments</h2>
      <table>
        <thead><tr><th>When</th><th>Patient</th><th>Provider</th><th>Office</th><th>Visit</th><th>Status</th></tr></thead>
        <tbody>
          {state.appointments.map((a) => (
            <tr key={a.appointment_id} className={fresh.has(a.appointment_id) ? "new" : ""}>
              <td className={a.status}>{a.day} {a.start.slice(5, 10)} {a.start.slice(11)}</td>
              <td>{a.patient_name}</td><td>{a.provider_name}</td><td>{a.office_name}</td>
              <td>{a.appointment_type}</td><td className={a.status}>{a.status}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Audit log (every tool call, including refused ones)</h2>
      <table>
        <thead><tr><th>Time</th><th>Tool</th><th>Result</th><th>Arguments</th></tr></thead>
        <tbody>
          {state.audit.map((e) => (
            <tr key={e.id}>
              <td>{new Date(e.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
              <td>{e.tool}</td>
              <td className={e.ok ? "ok" : "err"}>{e.ok ? "ok" : e.error_code}</td>
              <td><code>{JSON.stringify(e.args)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
