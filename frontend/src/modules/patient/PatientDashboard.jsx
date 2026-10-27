import { useEffect, useState } from "react";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || "/api";

const patientSections = [
  { id: "overview", label: "Overview", icon: "overview" },
  { id: "medical-records", label: "My Medical Records", icon: "records" },
  { id: "ask-assistant", label: "Ask AI Assistant", icon: "assistant" },
  { id: "request-history", label: "AI Request History", icon: "history" },
  { id: "profile", label: "My Profile", icon: "profile" },
];

const patientPlaceholders = {
  "medical-records": {
    eyebrow: "YOUR HEALTH INFORMATION",
    title: "My Medical Records",
    description:
      "A secure view of your medical records will be available in a future phase.",
    icon: "records",
  },
  "ask-assistant": {
    eyebrow: "PATIENT SUPPORT",
    title: "Ask AI Assistant",
    description:
      "The AI Assistant is not available yet. This space will be introduced in a future phase.",
    icon: "assistant",
  },
  "request-history": {
    eyebrow: "YOUR ACTIVITY",
    title: "AI Request History",
    description:
      "Your AI request history is not available yet and will be introduced in a future phase.",
    icon: "history",
  },
  profile: {
    eyebrow: "ACCOUNT DETAILS",
    title: "My Profile",
    description:
      "Profile management is not available yet. Your account details will be available here in a future phase.",
    icon: "profile",
  },
};

function PatientIcon({ name }) {
  const iconContent = {
    overview: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></>,
    records: <><path d="M7 3.5h7l4 4V20a1 1 0 0 1-1 1H7a2 2 0 0 1-2-2V5.5a2 2 0 0 1 2-2Z" /><path d="M14 3.5v5h5M8.5 13h7m-7 3.5h7" /></>,
    assistant: <><path d="M12 3.5a8.5 8.5 0 0 0-7.3 12.9L4 20l3.8-.8A8.5 8.5 0 1 0 12 3.5Z" /><path d="M12 7.5v.1m0 3v5" /></>,
    history: <><path d="M4.5 8.5A8 8 0 1 1 4 13" /><path d="M4.5 4.5v4h4m3.5-.5v5l3 2" /></>,
    profile: <><circle cx="12" cy="8" r="3.5" /><path d="M5 20a7 7 0 0 1 14 0" /></>,
  };

  return (
    <svg
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
      viewBox="0 0 24 24"
    >
      {iconContent[name]}
    </svg>
  );
}

function PatientDashboard({
  user,
  session,
  dashboardError,
  onLogout,
  signOutError,
}) {
  const [activeSection, setActiveSection] = useState("overview");
  const [recordState, setRecordState] = useState({
    status: "loading",
    record: null,
    error: "",
  });
  const [recordRetry, setRecordRetry] = useState(0);
  const firstName = user.name.trim().split(/\s+/)[0];
  const initials = user.name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();

  useEffect(() => {
    let active = true;

    async function loadRecord() {
      setRecordState({ status: "loading", record: null, error: "" });
      try {
        const response = await fetch(`${apiBaseUrl}/patient/my-record`, {
          headers: {
            Authorization: `Bearer ${session.access_token}`,
          },
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(
            result.detail || "Could not load your medical record.",
          );
        }
        if (
          !result ||
          typeof result !== "object" ||
          !Object.hasOwn(result, "record") ||
          (result.record !== null &&
            (!result.record ||
              typeof result.record !== "object" ||
              Array.isArray(result.record)))
        ) {
          throw new Error("The medical-record response was invalid.");
        }
        if (active) {
          setRecordState({
            status: result.record === null ? "empty" : "ready",
            record: result.record,
            error: "",
          });
        }
      } catch (error) {
        if (active) {
          setRecordState({
            status: "error",
            record: null,
            error:
              error instanceof Error
                ? error.message
                : "Could not load your medical record.",
          });
        }
      }
    }

    void loadRecord();
    return () => {
      active = false;
    };
  }, [recordRetry, session.access_token]);

  return (
    <main className="patient-shell">
      <aside className="patient-sidebar">
        <a className="brand patient-brand" href="/" aria-label="Cognitive Hospital home">
          <span className="patient-brand-mark" aria-hidden="true">+</span>
          <span className="brand-name">Cognitive<span>Hospital</span></span>
        </a>
        <p className="patient-workspace-label">YOUR CARE WORKSPACE</p>
        <nav className="patient-navigation" aria-label="Patient dashboard sections">
          {patientSections.map((section) => (
            <a
              className={`patient-nav-link${section.id === activeSection ? " patient-nav-link-active" : ""}`}
              href={`#${section.id}`}
              key={section.id}
              aria-current={section.id === activeSection ? "location" : undefined}
              onClick={() => setActiveSection(section.id)}
            >
              <PatientIcon name={section.icon} />
              <span>{section.label}</span>
            </a>
          ))}
        </nav>
        <div className="patient-sidebar-account">
          <span className="patient-avatar" aria-hidden="true">{initials}</span>
          <span className="patient-account-copy">
            <strong>{user.name}</strong>
            <span>Patient account</span>
          </span>
        </div>
      </aside>

      <section className="patient-main">
        <header className="patient-topbar">
          <span className="patient-breadcrumb">Workspace / <strong>Patient</strong></span>
          <div className="patient-account-actions">
            <span className="patient-role-badge">Patient</span>
            <button className="patient-sign-out" onClick={onLogout} type="button">
              Sign out
            </button>
          </div>
        </header>

        <div className="patient-content">
          {dashboardError && <p className="auth-error" role="alert">{dashboardError}</p>}
          {signOutError && <p className="auth-error" role="alert">{signOutError}</p>}

          <section className="patient-welcome" id="overview" aria-labelledby="patient-welcome-title">
            <div className="patient-welcome-copy">
              <p className="patient-eyebrow">A SPACE FOR YOUR CARE JOURNEY</p>
              <h1 id="patient-welcome-title">Welcome, {firstName}.</h1>
              <p>
                Your personal healthcare workspace is ready. Explore the sections
                to see what is available now and what is coming soon.
              </p>
            </div>
            <div className="patient-welcome-art" aria-hidden="true">
              <span className="patient-art-orbit patient-art-orbit-one" />
              <span className="patient-art-orbit patient-art-orbit-two" />
              <span className="patient-art-heart">+</span>
              <span className="patient-art-spark">✦</span>
            </div>
          </section>

          <section className="patient-overview-card" aria-label="Dashboard overview">
            <div className="patient-overview-icon" aria-hidden="true">
              <PatientIcon name="overview" />
            </div>
            <div>
              <p className="patient-eyebrow">OVERVIEW</p>
              <h2>Your dashboard, at a glance</h2>
              <p>
                This is the starting point for your records, assistant, request
                history, and profile. Features will appear here as they become
                available.
              </p>
            </div>
            <span className="patient-coming-badge">More features coming soon</span>
          </section>

          <div className="patient-section-grid">
            {patientSections.slice(1).map((section) => {
              const placeholder = patientPlaceholders[section.id];
              return (
                <section
                  className={`patient-feature-card patient-feature-${section.id}`}
                  id={section.id}
                  key={section.id}
                  aria-labelledby={`${section.id}-title`}
                >
                  <div className="patient-feature-icon" aria-hidden="true">
                    <PatientIcon name={placeholder.icon} />
                  </div>
                  <p className="patient-eyebrow">{placeholder.eyebrow}</p>
                  <h2 id={`${section.id}-title`}>{placeholder.title}</h2>
                  {section.id === "medical-records" ? (
                    <>
                      {recordState.status === "loading" && (
                        <p className="patient-record-status" role="status">
                          Loading your medical record…
                        </p>
                      )}
                      {recordState.status === "empty" && (
                        <p className="patient-record-status" role="status">
                          No medical record available.
                        </p>
                      )}
                      {recordState.status === "error" && (
                        <div className="patient-record-error" role="alert">
                          <p>{recordState.error}</p>
                          <button
                            className="patient-retry-button"
                            onClick={() => setRecordRetry((current) => current + 1)}
                            type="button"
                          >
                            Try again
                          </button>
                        </div>
                      )}
                      {recordState.status === "ready" && (
                        <dl className="patient-record-details">
                          <div>
                            <dt>Age</dt>
                            <dd>{recordState.record.age ?? "Not provided"}</dd>
                          </div>
                          <div>
                            <dt>Medical condition</dt>
                            <dd>{recordState.record.medical_condition ?? "Not provided"}</dd>
                          </div>
                          <div>
                            <dt>Treatment</dt>
                            <dd>{recordState.record.treatment ?? "Not provided"}</dd>
                          </div>
                        </dl>
                      )}
                    </>
                  ) : (
                    <>
                      <p>{placeholder.description}</p>
                      <span className="patient-placeholder-label">Coming in a future phase</span>
                    </>
                  )}
                </section>
              );
            })}
          </div>
          <footer className="patient-footer">
            Patient workspace · Your account information is verified securely.
          </footer>
        </div>
      </section>
    </main>
  );
}

export default PatientDashboard;
