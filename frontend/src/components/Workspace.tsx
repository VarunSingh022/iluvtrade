import { useState } from "react";

import { Banner, Card, Field, Loading } from "./ui";
import { ApiError, api } from "../lib/api";
import type { Invitation, InvitationCreated, Member, MembershipSummary } from "../lib/api";
import { useAuth } from "../lib/auth";
import { relative } from "../lib/format";
import { useAsync } from "../lib/useAsync";

const ROLES = ["viewer", "trader", "admin", "owner"] as const;

/**
 * The workspace as a team.
 *
 * The invitation token is shown to the **inviter**, once, because this
 * deployment has no email channel and the alternative would be to claim an
 * email was sent. That is a product decision with a visible consequence, so
 * the screen states it rather than leaving the user to wonder where the
 * invitation went.
 */
export default function Workspace() {
  const { user, switchOrganization } = useAuth();
  const members = useAsync<Member[]>(() => api.get<Member[]>("/organizations/members"), []);
  const workspaces = useAsync<MembershipSummary[]>(
    () => api.get<MembershipSummary[]>("/auth/organizations"),
    [],
  );
  const canAdminister = user?.role === "admin" || user?.role === "owner";
  const invitations = useAsync<Invitation[]>(
    () => (canAdminister ? api.get<Invitation[]>("/organizations/invitations") : Promise.resolve([])),
    [canAdminister],
  );

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<string>("trader");
  const [issued, setIssued] = useState<InvitationCreated | undefined>();
  const [joinToken, setJoinToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();

  function fail(caught: unknown) {
    setError(caught instanceof ApiError ? caught.message : String(caught));
  }

  async function invite() {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      setIssued(await api.post<InvitationCreated>("/organizations/invitations", { email, role }));
      setEmail("");
      invitations.reload();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    setBusy(true);
    setError(undefined);
    try {
      await api.post<Invitation>(`/organizations/invitations/${id}/revoke`);
      invitations.reload();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  async function join() {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const joined = await api.post<Member>("/organizations/invitations/accept", { token: joinToken });
      setJoinToken("");
      setNotice(
        `Joined as ${joined.role}. Switch to the workspace below to start working in it.`,
      );
      workspaces.reload();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  async function moveTo(organizationId: string) {
    setBusy(true);
    setError(undefined);
    try {
      await switchOrganization(organizationId);
      members.reload();
      workspaces.reload();
      invitations.reload();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  }

  if (!user) return <Loading />;

  return (
    <>
      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="ok">{notice}</Banner>}

      <div className="grid cols-2">
        <Card title="Members">
          {members.loading && !members.data ? (
            <Loading />
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Joined</th></tr></thead>
                <tbody>
                  {(members.data ?? []).map((member) => (
                    <tr key={member.user_id}>
                      <td className="small">{member.display_name}</td>
                      <td className="tiny faint">{member.email}</td>
                      <td><span className="badge neutral">{member.role}</span></td>
                      <td className="tiny faint">{relative(member.joined_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Your workspaces">
          <div className="table-wrap">
            <table>
              <tbody>
                {(workspaces.data ?? []).map((workspace) => (
                  <tr key={workspace.organization_id}>
                    <td className="small">{workspace.organization_name}</td>
                    <td><span className="badge neutral">{workspace.role}</span></td>
                    <td style={{ textAlign: "right" }}>
                      {workspace.is_current ? (
                        <span className="badge ok">current</span>
                      ) : (
                        <button
                          className="small"
                          disabled={busy}
                          onClick={() => void moveTo(workspace.organization_id)}
                          type="button"
                        >
                          Switch to
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="tiny faint" style={{ marginBottom: 0 }}>
            Switching issues a new session. A session belongs to one workspace, so everything you
            do is unambiguously attributed to it.
          </p>

          <hr />
          <Field label="Have an invitation code?" hint="Someone in another workspace gave you this.">
            <input value={joinToken} onChange={(event) => setJoinToken(event.target.value)} autoComplete="off" />
          </Field>
          <button disabled={busy || !joinToken} onClick={() => void join()} type="button">
            Join workspace
          </button>
        </Card>
      </div>

      {canAdminister && (
        <Card title="Invitations">
          {issued && (
            <>
              <Banner tone="warn">{issued.share_instructions}</Banner>
              <div className="secret-box" data-testid="invitation-token">{issued.token}</div>
            </>
          )}

          <div className="row" style={{ alignItems: "flex-end", gap: "0.6rem" }}>
            <div style={{ flex: 1 }}>
              <Field label="Email address">
                <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} />
              </Field>
            </div>
            <div>
              <Field label="Role">
                <select value={role} onChange={(event) => setRole(event.target.value)}>
                  {ROLES.filter((candidate) =>
                    // An invitation can never grant more than the inviter
                    // holds; the server enforces it, and offering the option
                    // only to have it refused is a worse way to say so.
                    user.role === "owner" ? true : candidate !== "owner",
                  ).map((candidate) => (
                    <option key={candidate} value={candidate}>{candidate}</option>
                  ))}
                </select>
              </Field>
            </div>
            <button className="primary" disabled={busy || !email} onClick={() => void invite()} type="button">
              Invite
            </button>
          </div>

          {(invitations.data ?? []).length === 0 ? (
            <p className="dim small" style={{ marginBottom: 0 }}>No invitations yet.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Email</th><th>Role</th><th>Status</th><th>Expires</th><th /></tr></thead>
                <tbody>
                  {(invitations.data ?? []).map((invitation) => (
                    <tr key={invitation.id}>
                      <td className="small">{invitation.email}</td>
                      <td><span className="badge neutral">{invitation.role}</span></td>
                      <td>
                        <span className={`badge ${invitation.status === "accepted" ? "ok" : invitation.status === "pending" ? "warn" : "neutral"}`}>
                          {invitation.status}
                        </span>
                      </td>
                      <td className="tiny faint">{relative(invitation.expires_at)}</td>
                      <td style={{ textAlign: "right" }}>
                        {invitation.status === "pending" && (
                          <button className="small" disabled={busy} onClick={() => void revoke(invitation.id)} type="button">
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
