import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import { Chip, Panel } from "../../shared/workspaceUi.jsx";

/** Placeholder until the NeMo-guarded assistant is added (Checkpoint 4). */
export default function PatientAssistantPage() {
  return (
    <>
      <PageHeader
        description="A guarded assistant for questions about the record-correction process."
        eyebrow="PATIENT SUPPORT"
        icon="assistant"
        title="Ask AI Assistant"
      />
      <Panel
        aside={<Chip>Coming next phase</Chip>}
        eyebrow="NOT YET AVAILABLE"
        icon="assistant"
        title="The assistant is not active yet"
        titleId="assistant-title"
        tone={3}
      >
        <div className="ws-placeholder">
          <p>
            When it launches, it will answer general questions about how
            correction requests work. Every message will pass through safety
            guardrails, and it will never see your medical record.
          </p>
          <p className="ws-form-note">No AI responses are shown until the assistant is available.</p>
        </div>
      </Panel>
    </>
  );
}
