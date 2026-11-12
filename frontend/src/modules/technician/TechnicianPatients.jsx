import { useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import { useRemote } from "../../shared/useRemote.js";
import {
  EmptyMessage,
  ErrorMessage,
  LoadingMessage,
  Panel,
  RefreshButton,
} from "../../shared/workspaceUi.jsx";
import { listPatients } from "./technicianApi.js";

export default function TechnicianPatientsPage() {
  const { session } = useOutletContext();
  const patients = useRemote(() => listPatients(session.access_token), [session.access_token]);

  return (
    <>
      <PageHeader
        actions={<RefreshButton busy={patients.status === "loading"} label="Refresh" onClick={patients.reload} />}
        description="Only patients assigned to you are shown. This summary is read-only: diagnoses, medications and treatment plans are changed by physicians."
        eyebrow="CARE TEAM"
        icon="users"
        title="Assigned Patients"
      />

      <Panel
        eyebrow="READ-ONLY"
        icon="records"
        subtitle="Patient summaries for your care assignments."
        title="Your patients"
        titleId="patients-title"
        tone={1}
      >
        {patients.status === "loading" && <LoadingMessage />}
        {patients.status === "error" && <ErrorMessage message={patients.error} onRetry={patients.reload} />}
        {patients.status === "ready" && patients.data.length === 0 && (
          <EmptyMessage>No patients are assigned to you yet.</EmptyMessage>
        )}
        {patients.status === "ready" && patients.data.length > 0 && (
          <div className="ws-table-wrap">
            <table className="ws-table">
              <caption className="ws-visually-hidden">Patients assigned to you</caption>
              <thead>
                <tr>
                  <th scope="col">Patient</th>
                  <th scope="col">ID</th>
                  <th scope="col">Age</th>
                  <th scope="col">Condition</th>
                  <th scope="col">Treatment</th>
                </tr>
              </thead>
              <tbody>
                {patients.data.map((patient) => (
                  <tr key={patient.patient_id}>
                    <td>{patient.name ?? "—"}</td>
                    <td>{patient.patient_id}</td>
                    <td>{patient.age ?? "—"}</td>
                    <td>{patient.medical_condition ?? "—"}</td>
                    <td>{patient.treatment ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
