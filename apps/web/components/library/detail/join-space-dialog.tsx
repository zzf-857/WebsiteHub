"use client";

import { Box, Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { LibraryDialog } from "@/components/library/library-dialog";
import { Spinner } from "@/components/react-bits/spinner";
import { addSpaceMember, listSpaces, SpaceApiError } from "@/lib/space-client";
import type { Space } from "@/lib/space-contract";

/* 设计稿 1d 的「加入 Space」按钮对应的选择弹层。
   加入走 addSpaceMember(spaceId, { expectedVersion, siteId })，
   expectedVersion 用列表里拿到的 space.version 做乐观并发控制；
   版本冲突时刷新列表让用户基于最新版本重试。 */

type JoinSpaceDialogProps = {
  open: boolean;
  siteId: string;
  siteName: string;
  onClose: () => void;
  onJoined: (spaceName: string) => void;
};

type SpacesState =
  | { status: "loading" }
  | { status: "ready"; spaces: Space[] }
  | { status: "error"; message: string };

export function JoinSpaceDialog({
  open,
  siteId,
  siteName,
  onClose,
  onJoined,
}: Readonly<JoinSpaceDialogProps>) {
  const [state, setState] = useState<SpacesState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [joiningId, setJoiningId] = useState<string | null>(null);
  const [joinError, setJoinError] = useState<string | null>(null);

  // 初始态即 loading；重新打开弹层时由父组件换 key 重挂载来回到 loading 态，
  // 重试在点击事件里先置回 loading，效果内不做同步 setState
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();

    void listSpaces({ sort: "updated", direction: "desc", limit: 50 }, controller.signal)
      .then((page) => setState({ status: "ready", spaces: page.items }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Space 列表加载失败，请稍后重试。",
        });
      });

    return () => controller.abort();
  }, [attempt, open]);

  const retry = () => {
    setState({ status: "loading" });
    setJoinError(null);
    setAttempt((current) => current + 1);
  };

  const join = async (space: Space) => {
    if (joiningId) return;
    setJoiningId(space.id);
    setJoinError(null);
    try {
      const result = await addSpaceMember(space.id, { expectedVersion: space.version, siteId });
      onJoined(result.space.name);
    } catch (error) {
      if (error instanceof SpaceApiError && error.code === "version_conflict") {
        // 该 Space 在别处刚被更新，刷新列表拿到新 version 后再让用户重试
        setJoinError("该 Space 刚被其他操作更新，列表已刷新，请重试。");
        setState({ status: "loading" });
        setAttempt((current) => current + 1);
      } else {
        setJoinError(error instanceof Error ? error.message : "加入失败，请稍后重试。");
      }
    } finally {
      setJoiningId(null);
    }
  };

  return (
    <LibraryDialog
      open={open}
      title="加入 Space"
      description={`选择要收纳「${siteName}」的 Space。`}
      closeDisabled={joiningId !== null}
      onClose={onClose}
    >
      {state.status === "loading" && (
        <p className="sd-hint" role="status">
          <Spinner size={16} />
          正在加载 Space 列表…
        </p>
      )}

      {state.status === "error" && (
        <div className="sd-inline-error" role="alert">
          <p>{state.message}</p>
          <button type="button" className="sd-btn sd-btn-secondary sd-btn-small" onClick={retry}>
            <RefreshCw size={16} aria-hidden="true" />
            重试
          </button>
        </div>
      )}

      {state.status === "ready" &&
        (state.spaces.length > 0 ? (
          <ul className="sd-space-list">
            {state.spaces.map((space) => (
              <li key={space.id} className="sd-space-item">
                <Box size={16} aria-hidden="true" />
                <div className="sd-space-body">
                  <div className="sd-space-name">{space.name}</div>
                  <div className="sd-space-count">{space.memberCount} 个网站</div>
                </div>
                <button
                  type="button"
                  className="sd-btn sd-btn-secondary sd-btn-small"
                  onClick={() => void join(space)}
                  disabled={joiningId !== null}
                  aria-busy={joiningId === space.id}
                >
                  {joiningId === space.id ? <Spinner size={16} /> : <Plus size={16} aria-hidden="true" />}
                  {joiningId === space.id ? "正在加入" : "加入"}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="sd-hint-block">
            <p className="sd-hint">还没有创建任何 Space。</p>
            <Link className="sd-btn sd-btn-secondary sd-btn-small" href="/spaces">
              去创建 Space
            </Link>
          </div>
        ))}

      {joinError && (
        <p className="sd-form-error" role="alert">
          {joinError}
        </p>
      )}
    </LibraryDialog>
  );
}
