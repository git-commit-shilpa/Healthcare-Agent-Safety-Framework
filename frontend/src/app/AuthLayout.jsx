import { HasfLogo } from "../shared/WorkspaceShell.jsx";

const networkNodes = [
  [70, 90, "a"], [190, 60, "b"], [300, 130, "c"], [120, 210, "b"], [250, 240, "a"],
  [360, 260, "c"], [80, 330, "c"], [210, 360, "a"], [330, 390, "b"],
];
const networkLinks = [
  [0, 1], [1, 2], [0, 3], [1, 4], [2, 4], [2, 5], [3, 4], [3, 6], [4, 7], [5, 8], [6, 7], [7, 8], [4, 5],
];

function SafetyNetwork() {
  return (
    <svg aria-hidden="true" className="auth-network" focusable="false" viewBox="0 0 430 450">
      {networkLinks.map(([from, to], index) => (
        <line
          className="auth-network-link"
          key={`${from}-${to}`}
          style={{ animationDelay: `${(index % 5) * 0.7}s` }}
          x1={networkNodes[from][0]}
          x2={networkNodes[to][0]}
          y1={networkNodes[from][1]}
          y2={networkNodes[to][1]}
        />
      ))}
      {networkNodes.map(([x, y, tone], index) => (
        <g key={`${x}-${y}`}>
          <circle className={`auth-network-halo auth-network-${tone}`} cx={x} cy={y} r="15" style={{ animationDelay: `${index * 0.45}s` }} />
          <circle className={`auth-network-node auth-network-${tone}`} cx={x} cy={y} r="6" />
        </g>
      ))}
      <g className="auth-network-shield" transform="translate(178 168)">
        <path d="M37 0 74 15v26c0 24-16 41-37 49C16 82 0 65 0 41V15L37 0Z" />
        <path className="auth-network-check" d="m22 44 10 10 21-22" />
      </g>
    </svg>
  );
}

/**
 * Split-screen layout for sign-in pages. The form comes first in the DOM so
 * keyboard and screen-reader users reach it immediately.
 */
export function AuthLayout({ children }) {
  return (
    <div className="auth-split">
      <main className="auth-panel">
        <div className="auth-form-card">{children}</div>
      </main>

      <aside className="auth-visual" aria-label="About the Healthcare Agent Safety Framework">
        <div className="auth-visual-backdrop" aria-hidden="true">
          <span className="auth-orb auth-orb-one" />
          <span className="auth-orb auth-orb-two" />
          <span className="auth-orb auth-orb-three" />
          <span className="auth-grid" />
        </div>
        <div className="auth-visual-content">
          <div className="auth-brand">
            <HasfLogo />
            <span>
              <strong>HASF</strong>
              <span>Healthcare Agent Safety Framework</span>
            </span>
          </div>
          <p className="auth-kicker">COGNITIVE HOSPITAL · CLINICAL AI SAFETY</p>
          <p className="auth-headline">Every AI action is authorized before it runs.</p>
          <p className="auth-summary">
            HASF checks each AI request against the signed-in role and its
            permissions, applies safety guardrails, and sends sensitive changes
            to a human reviewer. Every decision is recorded for audit.
          </p>
          <SafetyNetwork />
          <ul className="auth-pillars">
            <li><span className="auth-pillar-dot auth-pillar-rbac" aria-hidden="true" />Role-based access for every request</li>
            <li><span className="auth-pillar-dot auth-pillar-guard" aria-hidden="true" />Allow · Block · Escalate guardrail decisions</li>
            <li><span className="auth-pillar-dot auth-pillar-human" aria-hidden="true" />Human-in-the-loop review and audit trail</li>
          </ul>
          <p className="auth-roles" aria-label="Supported roles">
            <span>Admin</span><span>Patient</span><span>Physician</span><span>Technician</span>
          </p>
        </div>
      </aside>
    </div>
  );
}
