import { useState } from "react";
import { Alert, Empty, Modal, Select, Typography } from "antd";
import type { GroupSummary } from "../../api";
import { href } from "../../hooks/useRoute";
import { useGroupList } from "../groups/useGroups";

const { Text } = Typography;

/**
 * Add group: choose a group to give access to this agent. The capabilities are
 * chosen next, in the same drawer as a person's. Groups that already hold
 * access here are edited from the list instead.
 */
export function GroupPicker({
  open,
  hubName,
  exclude,
  onClose,
  onPick,
}: {
  open: boolean;
  hubName: string;
  /** Group ids already listed in this agent's access. */
  exclude: string[];
  onClose: () => void;
  onPick: (group: GroupSummary) => void;
}) {
  const list = useGroupList(open);
  const [value, setValue] = useState<string | undefined>();
  const groups = (list.data ?? []).filter((g) => !exclude.includes(g.id));
  const chosen = groups.find((g) => g.id === value);
  return (
    <Modal
      title={`Give a group access to ${hubName}`}
      open={open}
      okText="Choose capabilities"
      okButtonProps={{ disabled: !chosen }}
      onOk={() => {
        if (!chosen) return;
        setValue(undefined);
        onPick(chosen);
      }}
      onCancel={() => {
        setValue(undefined);
        onClose();
      }}
      destroyOnHidden
    >
      {list.error ? (
        <Alert type="error" showIcon title="Couldn’t load groups" description={list.error} />
      ) : list.data && groups.length === 0 ? (
        <Empty
          description={
            list.data.length
              ? "Every group already has access here. Edit a group from the access list."
              : "No groups yet."
          }
        >
          <a href={href("/groups")}>Manage groups</a>
        </Empty>
      ) : (
        <div className="field">
          <label className="field-label" htmlFor="pick-group">
            Group
          </label>
          <Select
            id="pick-group"
            showSearch={{ optionFilterProp: "label" }}
            loading={!list.data}
            placeholder="Choose a group"
            value={value}
            onChange={setValue}
            options={groups.map((g) => ({
              value: g.id,
              label: `${g.name} (${g.member_count} ${g.member_count === 1 ? "member" : "members"})`,
            }))}
          />
          <Text type="secondary" className="field-help">
            Everyone in the group gets the access you choose next. <a href={href("/groups")}>Manage groups</a>
          </Text>
        </div>
      )}
    </Modal>
  );
}
