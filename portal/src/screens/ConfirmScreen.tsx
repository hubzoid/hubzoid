import { useState } from "react";
import { Alert, App, Button, Descriptions, Space, Tag, Typography } from "antd";
import { ApiError, request, type ChangeRequest, type Hub } from "../api";
import { errorText, useData } from "../hooks/useData";
import { href, hrefWith, personHref } from "../hooks/useRoute";
import { LoadState, PageHeader } from "../components/common";
import { capabilityLabel, formatTime, humanize, relativeTime } from "../lib/format";
import { OneTimePassword, PasswordField } from "./people/AccountDrawer";
import { passwordProblem } from "./people/password";

const { Text, Paragraph } = Typography;

const SURFACES: Record<string, string> = {
  owui: "chat",
  web: "the web",
  api: "the API",
  mcp: "an MCP assistant",
  whatsapp: "WhatsApp",
  telegram: "Telegram",
  console: "the Console",
};

/**
 * Confirm or reject a change an agent proposed on your behalf. The server
 * shows it only to the person who proposed it, applies it only with the plan
 * hash shown here, and checks your access again when you confirm.
 */
export function ConfirmScreen({ id, hubs }: { id: string; hubs: Hub[] }) {
  const { modal } = App.useApp();
  const data = useData<ChangeRequest>(`/change-requests/${encodeURIComponent(id)}`);
  const [password, setPassword] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [done, setDone] = useState<"confirmed" | "rejected" | null>(null);
  const [shownPassword, setShownPassword] = useState("");

  const cr = data.data;
  if (!cr)
    return (
      <div className="panel">
        {data.status === 404 ? (
          <Alert
            type="warning"
            showIcon
            title="This change request isn’t available"
            description="It doesn’t exist, or it was proposed by someone else. Only the person who asked for a change can confirm it. Ask the agent again if you still need it."
          />
        ) : (
          <LoadState error={data.error} retry={data.reload} />
        )}
      </div>
    );

  const plan = cr.plan;
  const account = plan.kind === "account";
  const hubName = cr.hub_name || hubs.find((h) => h.key === cr.hub)?.name || cr.hub;
  const label = (p: string) => capabilityLabel(p, cr.labels);
  const person = plan.kind === "account" ? plan.email : plan.subject;
  const status = done ?? cr.status;
  const pending = status === "pending";
  // Compared with when the request was loaded; the server decides on confirm.
  const expired = pending && cr.expires * 1000 < (data.at ?? 0);

  async function confirm() {
    if (!cr) return;
    setTouched(true);
    if (account && passwordProblem(password)) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/change-requests/${encodeURIComponent(cr.id)}/confirm`, {
        plan_hash: cr.plan_hash,
        ...(account ? { password } : {}),
      });
      if (account) setShownPassword(password);
      setPassword("");
      setDone("confirmed");
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(errorText(e), 0));
      data.reload();
    } finally {
      setBusy(false);
    }
  }

  function reject() {
    if (!cr) return;
    modal.confirm({
      title: "Reject this change?",
      content: "Nothing changes. The request can’t be used afterwards.",
      okText: "Reject",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await request(`/change-requests/${encodeURIComponent(cr.id)}/reject`, {});
          setPassword("");
          setDone("rejected");
        } catch (e) {
          setError(e instanceof ApiError ? e : new ApiError(errorText(e), 0));
          data.reload();
        }
      },
    });
  }

  return (
    <>
      <PageHeader
        eyebrow="Confirm a change"
        title={account ? "Create an account" : "Change access"}
        description={`Proposed from ${SURFACES[cr.surface ?? ""] ?? humanize(cr.surface ?? "an agent")} ${relativeTime(cr.created)}. Nothing has changed yet.`}
      />
      <div className="panel">
        {status === "confirmed" && (
          <Alert
            type="success"
            showIcon
            title={account ? "Account created" : "Access updated"}
            description={
              <>
                {account ? `${plan.kind === "account" ? plan.name : person} can now sign in.` : `The change for ${person} is in effect.`}{" "}
                <a href={personHref(person)}>Open their details</a>
              </>
            }
          />
        )}
        {status === "confirmed" && shownPassword && <OneTimePassword password={shownPassword} />}
        {status === "rejected" && <Alert type="info" showIcon title="Rejected. Nothing changed." />}
        {(status === "expired" || expired) && (
          <Alert type="warning" showIcon title="This request expired" description="Nothing changed. Ask the agent again for a new link." />
        )}
        {status === "failed" && (
          <Alert type="error" showIcon title="This request could not be applied" description={cr.result || "Nothing changed. Ask the agent again."} />
        )}
        {status === "applying" && (
          <Alert type="warning" showIcon title="This request is being applied" description="Reload in a moment to see the result." />
        )}
        {error && <Alert type={error.certain ? "error" : "warning"} showIcon title={error.certain ? "Not applied" : "Couldn’t confirm the result"} description={error.message} />}
        {pending && !expired && cr.problem && (
          <Alert
            type="warning"
            showIcon
            title="This change can’t be applied as it stands"
            description={cr.problem}
          />
        )}

        <Descriptions
          size="small"
          column={1}
          style={{ marginTop: 16 }}
          items={[
            { key: "agent", label: "Agent", children: <a href={hrefWith(`/agents/${encodeURIComponent(cr.hub)}/access`, {})}>{hubName}</a> },
            {
              key: "person",
              label: "Person",
              children: (
                <span>
                  {plan.kind === "account" && <Text strong>{plan.name} </Text>}
                  <span className="identity">{person}</span>
                </span>
              ),
            },
            ...(plan.kind === "account"
              ? [{ key: "account", label: "Chat account", children: "New sign-in with the normal user role" }]
              : [
                  {
                    key: "current",
                    label: "Holds now",
                    children: cr.current?.length ? (
                      <Space wrap size={[6, 6]}>
                        {cr.current.map((p) => (
                          <Tag key={p}>{label(p)}</Tag>
                        ))}
                      </Space>
                    ) : (
                      <Text type="secondary">No direct access</Text>
                    ),
                  },
                ]),
            {
              key: "allow",
              label: "Allow",
              children: plan.grant.length ? (
                <Space wrap size={[6, 6]}>
                  {plan.grant.map((p) => (
                    <Tag key={p} color={cr.labels[p]?.sensitive ? "orange" : "blue"}>
                      {label(p)}
                    </Tag>
                  ))}
                </Space>
              ) : (
                <Text type="secondary">Nothing</Text>
              ),
            },
            ...(plan.kind === "access"
              ? [
                  {
                    key: "remove",
                    label: "Remove",
                    children: plan.revoke.length ? (
                      <Space wrap size={[6, 6]}>
                        {plan.revoke.map((p) => (
                          <Tag key={p} color="red">
                            {label(p)}
                          </Tag>
                        ))}
                      </Space>
                    ) : (
                      <Text type="secondary">Nothing</Text>
                    ),
                  },
                ]
              : []),
            { key: "expires", label: "Expires", children: <Text>{formatTime(cr.expires)}</Text> },
          ]}
        />
        {plan.kind === "access" && plan.revoke.includes("use_hub") && (
          <Alert
            type="warning"
            showIcon
            title={`Removing “${label("use_hub")}” removes every capability ${person} holds in ${hubName}`}
          />
        )}

        {pending && !expired && (
          <div className="section" style={{ marginTop: 16 }}>
            {account && (
              <>
                <Paragraph type="secondary" style={{ marginBottom: 8 }}>
                  Set their password here. The agent never sees it. It is shown once after the account is created.
                </Paragraph>
                <PasswordField id="confirm-password" value={password} onChange={setPassword} touched={touched} />
              </>
            )}
            <Space wrap>
              <Button type="primary" loading={busy} disabled={!!cr.problem} onClick={() => void confirm()}>
                {account ? "Create account" : "Apply change"}
              </Button>
              <Button disabled={busy} onClick={reject}>
                Reject
              </Button>
              <Button type="link" href={href("/people")}>
                Back to people
              </Button>
            </Space>
          </div>
        )}
      </div>
    </>
  );
}
