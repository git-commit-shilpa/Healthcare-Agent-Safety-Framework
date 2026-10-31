import { useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import { Badge, Chip, Panel } from "../../shared/workspaceUi.jsx";
import { PERMISSION_DESCRIPTIONS } from "./patientCorrection.js";

export default function PatientProfilePage() {
  const { user, recordState } = useOutletContext();
  const describedPermissions = user.permissions.filter(
    (permission) => PERMISSION_DESCRIPTIONS[permission],
  );
  const age =
    recordState.status === "ready"
      ? recordState.record.age ?? "Not provided"
      : recordState.status === "loading"
        ? "Loading…"
        : "Not available";

  return (
    <>
      <PageHeader
        description="Details from your verified sign-in. Contact your care team to change them."
        eyebrow="ACCOUNT DETAILS"
        icon="profile"
        title="My Profile"
      />

      <div className="ws-split">
        <Panel
          aside={<Chip>Read only</Chip>}
          eyebrow="IDENTITY"
          icon="profile"
          title="Account"
          titleId="profile-account-title"
          tone={1}
        >
          <dl className="ws-details">
            <div>
              <dt>Name</dt>
              <dd>{user.name}</dd>
            </div>
            <div>
              <dt>Email</dt>
              <dd>{user.email}</dd>
            </div>
            <div>
              <dt>Role</dt>
              <dd><Badge tone="role">Patient</Badge></dd>
            </div>
            <div>
              <dt>Account status</dt>
              <dd><Badge tone="active">Active</Badge></dd>
            </div>
            <div>
              <dt>Age</dt>
              <dd>{age}</dd>
            </div>
          </dl>
        </Panel>

        <Panel
          eyebrow="ACCESS & PRIVACY"
          icon="permissions"
          title="What your account can do"
          titleId="profile-access-title"
          tone={4}
        >
          {describedPermissions.length ? (
            <ul className="ws-capabilities">
              {describedPermissions.map((permission) => (
                <li key={permission}>{PERMISSION_DESCRIPTIONS[permission]}</li>
              ))}
            </ul>
          ) : (
            <p className="ws-message">No Patient features are enabled for this account.</p>
          )}
          <p className="ws-note">
            You can only see your own information. Your record can only be
            changed by your assigned Physician, and every request is logged for
            auditing.
          </p>
        </Panel>
      </div>
    </>
  );
}
