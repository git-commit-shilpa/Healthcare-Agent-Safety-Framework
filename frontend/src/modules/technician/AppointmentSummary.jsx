import { Badge } from "../../shared/workspaceUi.jsx";
import { formatDateTime } from "./technician.js";

const STATUS_TONE = {
  "Pending Confirmation": "pending",
  "Needs Information": "escalate",
  "Awaiting Physician": "escalate",
  Confirmed: "allow",
  Rejected: "block",
  Cancelled: "inactive",
  Completed: "approved",
};

/** The scheduling facts a Technician needs to decide, in one place. */
export default function AppointmentSummary({ task }) {
  const appointment = task.appointment;
  if (!appointment) return null;

  return (
    <section className="tc-ap-summary" aria-label="Appointment request">
      <div className="tc-ap-summary-head">
        <div>
          <p className="ws-eyebrow">APPOINTMENT REQUEST #{task.task_id}</p>
          <h4>{appointment.local_label}</h4>
        </div>
        <Badge tone={STATUS_TONE[appointment.status] ?? "neutral"}>{appointment.status_label}</Badge>
      </div>

      <dl className="tc-ap-facts">
        <div><dt>Patient</dt><dd>{task.patient.name ?? `Patient ${task.patient.patient_id}`}</dd></div>
        <div><dt>Doctor</dt><dd>{appointment.physician_name ?? "—"}</dd></div>
        <div><dt>Preferred time</dt><dd>{appointment.local_label}</dd></div>
        <div><dt>Doctor&apos;s time zone</dt><dd>{appointment.timezone}</dd></div>
        <div><dt>Requested</dt><dd>{formatDateTime(task.created_at) ?? "—"}</dd></div>
        <div><dt>Request status</dt><dd>{task.status}</dd></div>
      </dl>

      <div className="tc-ap-quote">
        <h5>Reason the patient gave</h5>
        <p>{appointment.reason ?? "No reason given."}</p>
      </div>

      {appointment.patient_response && (
        <div className="tc-ap-quote tc-ap-quote-reply">
          <h5>Patient&apos;s latest reply</h5>
          <p>{appointment.patient_response}</p>
        </div>
      )}
    </section>
  );
}
