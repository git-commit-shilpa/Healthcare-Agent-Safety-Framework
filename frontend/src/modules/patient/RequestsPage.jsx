import { useOutletContext } from "react-router-dom";

import { readFeatureFlags } from "../../app/features.js";
import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import { RefreshButton } from "../../shared/workspaceUi.jsx";
import { CorrectionRequestsPanel } from "./RequestHistorySection.jsx";
import PatientServiceRequestsPage from "./ServiceRequestsSection.jsx";

/**
 * One page for every non-appointment request: service requests and medical-record corrections
 * (including those raised through the AI Assistant). Appointments have their own page.
 */
export default function PatientRequestsPage() {
  const { historyState, refreshHistory, session } = useOutletContext();
  const { serviceRequests } = readFeatureFlags();

  return (
    <>
      <PageHeader
        actions={
          historyState.status !== "forbidden" && (
            <RefreshButton busy={historyState.status === "loading"} label="Refresh corrections" onClick={refreshHistory} />
          )
        }
        description="Service requests and medical-record corrections in one place. Corrections you raise with the AI Assistant appear here and are reviewed by your assigned Physician. Appointments are on the Appointments page."
        eyebrow="YOUR ACTIVITY"
        icon="history"
        title="Requests"
      />
      {serviceRequests && <PatientServiceRequestsPage embedded />}
      <CorrectionRequestsPanel historyState={historyState} refreshHistory={refreshHistory} token={session?.access_token} />
    </>
  );
}
