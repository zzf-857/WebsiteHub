"use client";

// 从 space-workspace.tsx 抽出来的有状态逻辑。
//
// 动机是可读性而不是行数：原组件把几百行 state/effect/handler 和同样量级的 JSX
// 摞在一个函数里，改一处标记要先翻过整套数据加载。分开之后，「数据怎么来」和
// 「长什么样」各看各的一个文件。
//
// 返回 15 个值看着多，但这就是原来那段逻辑本来就要交给 JSX 的东西——
// 拆分只是把这份契约显式写出来，没有新增耦合。

import {
  useRouter,
} from "next/navigation";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  type OpenAllTarget,
} from "@/components/spaces/open-all-dialog";
import {
  createSpace,
  DEFAULT_SPACE_MEMBER_PAGE_SIZE,
  DEFAULT_SPACE_PAGE_SIZE,
  deleteSpace,
  getSpace,
  listSpaces,
  previewSpaceDelete,
  removeSpaceMember,
  reorderSpaceMembers,
  updateSpace,
} from "@/lib/space-client";
import {
  type Space,
  type SpaceDetail,
  type SpaceDirection,
  type SpaceSort,
} from "@/lib/space-contract";
import {
  DialogState,
  EMPTY_SPACE_PAGE,
  MemberMove,
  SpacePageState,
  appendUniqueMembers,
  appendUniqueSpaces,
  errorMessage,
  isAbortError,
  isSpaceCode,
  isSpaceStatus,
} from "@/components/spaces/space-workspace-parts";

// 全站统一使用共享版网站图标（size 为像素值）；
// 旧枚举尺寸按 small=20 / medium=24 / large=32 迁移

export function useSpaceWorkspace(initialSpaceId: string | null) {
  const router = useRouter();
  const [sort, setSort] = useState<SpaceSort>("updated");
  const [direction, setDirection] = useState<SpaceDirection>("desc");
  const [spacePage, setSpacePage] = useState<SpacePageState>(EMPTY_SPACE_PAGE);
  const [spacesLoading, setSpacesLoading] = useState(true);
  const [spacesError, setSpacesError] = useState<string | null>(null);
  const [listRefreshVersion, setListRefreshVersion] = useState(0);
  const listGeneration = useRef(0);

  const [selectedSpaceId, setSelectedSpaceId] = useState<string | null>(initialSpaceId);
  const [detail, setDetail] = useState<SpaceDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailLoadingMore, setDetailLoadingMore] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRefreshVersion, setDetailRefreshVersion] = useState(0);
  const detailGeneration = useRef(0);

  const [dialog, setDialog] = useState<DialogState>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [memberBusyId, setMemberBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 队列 Q7：入口此前只在首页且只覆盖最近 8 个 Space，第 9 个起全站无入口。
  const [openAllTarget, setOpenAllTarget] = useState<OpenAllTarget | null>(null);

  useEffect(() => {
    if (initialSpaceId && !selectedSpaceId) {
      router.replace("/spaces");
    }
  }, [initialSpaceId, router, selectedSpaceId]);

  useEffect(() => {
    const controller = new AbortController();
    const generation = listGeneration.current + 1;
    listGeneration.current = generation;

    const load = async () => {
      setSpacesLoading(true);
      setSpacesError(null);
      setSpacePage(EMPTY_SPACE_PAGE);
      try {
        const result = await listSpaces({
          sort,
          direction,
          limit: DEFAULT_SPACE_PAGE_SIZE,
        }, controller.signal);
        if (controller.signal.aborted || listGeneration.current !== generation) return;
        setSpacePage({
          items: result.items,
          nextCursor: result.nextCursor,
          totalCount: result.aggregate.totalCount,
          loadingMore: false,
        });
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        if (listGeneration.current === generation) {
          setSpacesError(errorMessage(error, "Space 列表加载失败，请重试"));
        }
      } finally {
        if (!controller.signal.aborted && listGeneration.current === generation) {
          setSpacesLoading(false);
        }
      }
    };

    void load();
    return () => controller.abort();
  }, [direction, listRefreshVersion, sort]);

  useEffect(() => {
    if (!selectedSpaceId) return;

    const controller = new AbortController();
    const generation = detailGeneration.current + 1;
    detailGeneration.current = generation;

    const load = async () => {
      setDetailLoading(true);
      setDetailLoadingMore(false);
      setDetailError(null);
      try {
        const result = await getSpace(selectedSpaceId, {
          limit: DEFAULT_SPACE_MEMBER_PAGE_SIZE,
        }, controller.signal);
        if (controller.signal.aborted || detailGeneration.current !== generation) return;
        setDetail(result);
      } catch (error) {
        if (isAbortError(error) || controller.signal.aborted) return;
        if (detailGeneration.current !== generation) return;
        if (isSpaceStatus(error, 404)) {
          setSelectedSpaceId(null);
          setDetail(null);
          setNotice("该 Space 已不存在，列表已刷新");
          setListRefreshVersion((current) => current + 1);
        } else {
          setDetailError(errorMessage(error, "Space 内容加载失败，请重试"));
        }
      } finally {
        if (!controller.signal.aborted && detailGeneration.current === generation) {
          setDetailLoading(false);
        }
      }
    };

    void load();
    return () => controller.abort();
  }, [detailRefreshVersion, selectedSpaceId]);

  const refreshList = useCallback(() => {
    setListRefreshVersion((current) => current + 1);
  }, []);

  const refreshDetail = useCallback(() => {
    setDetailRefreshVersion((current) => current + 1);
  }, []);

  const closeDialog = () => {
    if (mutationBusy) return;
    setDialog(null);
    setMutationError(null);
  };

  const openDialog = (nextDialog: Exclude<DialogState, null>) => {
    setMutationError(null);
    setDialog(nextDialog);
  };

  const loadMoreSpaces = async () => {
    const cursor = spacePage.nextCursor;
    if (!cursor || spacePage.loadingMore) return;
    const generation = listGeneration.current;
    setSpacePage((current) => ({ ...current, loadingMore: true }));
    setSpacesError(null);
    try {
      const result = await listSpaces({
        sort,
        direction,
        cursor,
        limit: DEFAULT_SPACE_PAGE_SIZE,
      });
      if (listGeneration.current !== generation) return;
      setSpacePage((current) => ({
        items: appendUniqueSpaces(current.items, result.items),
        nextCursor: result.nextCursor,
        totalCount: result.aggregate.totalCount,
        loadingMore: false,
      }));
    } catch (error) {
      if (listGeneration.current !== generation) return;
      setSpacesError(errorMessage(error, "更多 Space 加载失败，请重试"));
      setSpacePage((current) => ({ ...current, loadingMore: false }));
    }
  };

  const loadMoreMembers = async (): Promise<SpaceDetail | null> => {
    if (!detail || detailLoadingMore) return detail;
    const cursor = detail.nextCursor;
    if (!cursor) return detail;
    const snapshot = detail;
    setDetailLoadingMore(true);
    setDetailError(null);
    try {
      const result = await getSpace(snapshot.id, {
        cursor,
        limit: DEFAULT_SPACE_MEMBER_PAGE_SIZE,
      });
      const merged: SpaceDetail = {
        ...result,
        members: appendUniqueMembers(snapshot.members, result.members),
      };
      setDetail((current) => (
        current?.id === snapshot.id && current.version === snapshot.version ? merged : current
      ));
      return merged;
    } catch (error) {
      if (isSpaceStatus(error, 409) || isSpaceStatus(error, 422)) {
        setNotice("Space 成员已发生变化，已重新加载最新顺序");
        refreshDetail();
      } else if (isSpaceStatus(error, 404)) {
        setSelectedSpaceId(null);
        setNotice("该 Space 已不存在，列表已刷新");
        refreshList();
      } else {
        setDetailError(errorMessage(error, "更多成员加载失败，请重试"));
      }
      return null;
    } finally {
      setDetailLoadingMore(false);
    }
  };

  const handleCreate = async (name: string) => {
    setMutationBusy(true);
    setMutationError(null);
    try {
      const created = await createSpace({ name });
      setDialog(null);
      setNotice(`已创建 Space“${created.name}”`);
      refreshList();
    } catch (error) {
      setMutationError(
        isSpaceCode(error, "duplicate_name")
          ? "当前账号中已存在同名 Space，请换一个名称。"
          : errorMessage(error, "创建 Space 失败，请重试"),
      );
    } finally {
      setMutationBusy(false);
    }
  };

  const resolveRenameConflict = async (space: Space) => {
    try {
      const latest = await getSpace(space.id, { limit: 1 });
      if (latest.version === space.version) {
        setMutationError("当前账号中已存在同名 Space，请换一个名称。");
      } else {
        setDialog({ kind: "rename", space: latest });
        setMutationError("该 Space 已被其他操作更新，已载入最新版本，请核对后再次保存。");
        if (selectedSpaceId === space.id) refreshDetail();
      }
    } catch (error) {
      if (isSpaceStatus(error, 404)) {
        setDialog(null);
        setNotice("该 Space 已被删除，列表已刷新");
      } else {
        setMutationError(errorMessage(error, "读取最新 Space 信息失败，请关闭后重试"));
      }
    } finally {
      refreshList();
    }
  };

  const handleRename = async (name: string) => {
    if (dialog?.kind !== "rename") return;
    const target = dialog.space;
    setMutationBusy(true);
    setMutationError(null);
    try {
      const updated = await updateSpace(target.id, {
        expectedVersion: target.version,
        name,
      });
      setDialog(null);
      setNotice(`Space 已重命名为“${updated.name}”`);
      refreshList();
      if (selectedSpaceId === target.id) refreshDetail();
    } catch (error) {
      if (isSpaceCode(error, "duplicate_name")) {
        setMutationError("当前账号中已存在同名 Space，请换一个名称。");
      } else if (isSpaceCode(error, "version_conflict")) {
        await resolveRenameConflict(target);
      } else if (isSpaceCode(error, "not_found")) {
        setDialog(null);
        setNotice("该 Space 已不存在，列表已刷新");
        refreshList();
        if (selectedSpaceId === target.id) setSelectedSpaceId(null);
      } else {
        setMutationError(errorMessage(error, "重命名失败，请重试"));
      }
    } finally {
      setMutationBusy(false);
    }
  };

  const loadDeletePreview = async (space: Space, conflictMessage?: string) => {
    setDialog({ kind: "delete", space, preview: null, previewLoading: true });
    setMutationError(conflictMessage ?? null);
    try {
      const preview = await previewSpaceDelete(space.id);
      setDialog((current) => (
        current?.kind === "delete" && current.space.id === space.id
          ? {
            kind: "delete",
            space: preview.space,
            preview,
            previewLoading: false,
          }
          : current
      ));
    } catch (error) {
      if (isSpaceStatus(error, 404)) {
        setDialog((current) => (
          current?.kind === "delete" && current.space.id === space.id ? null : current
        ));
        setNotice("该 Space 已不存在，列表已刷新");
        refreshList();
        if (selectedSpaceId === space.id) setSelectedSpaceId(null);
      } else {
        setDialog((current) => (
          current?.kind === "delete" && current.space.id === space.id
            ? { kind: "delete", space, preview: null, previewLoading: false }
            : current
        ));
        setMutationError((current) => (
          dialog?.kind === "delete" || !dialog
            ? errorMessage(error, "删除影响加载失败，请重试")
            : current
        ));
      }
    }
  };

  const handleDelete = async () => {
    if (dialog?.kind !== "delete" || !dialog.preview) return;
    const target = dialog.preview.space;
    setMutationBusy(true);
    setMutationError(null);
    try {
      const result = await deleteSpace(target.id, target.version);
      setDialog(null);
      setNotice(
        result.unlinkedSiteCount > 0
          ? `Space 已删除，${result.unlinkedSiteCount} 个网站仍保留在资料库中`
          : "Space 已删除",
      );
      if (selectedSpaceId === target.id) {
        setSelectedSpaceId(null);
        setDetail(null);
      }
      refreshList();
    } catch (error) {
      if (isSpaceCode(error, "version_conflict")) {
        await loadDeletePreview(
          target,
          "该 Space 已被其他操作更新，影响范围已刷新，请再次确认删除。",
        );
      } else if (isSpaceCode(error, "not_found")) {
        setDialog(null);
        setNotice("该 Space 已不存在，列表已刷新");
        if (selectedSpaceId === target.id) setSelectedSpaceId(null);
        refreshList();
      } else {
        setMutationError(errorMessage(error, "删除 Space 失败，请重试"));
      }
    } finally {
      setMutationBusy(false);
    }
  };

  const handleDetailMutationError = (error: unknown, fallback: string) => {
    if (
      isSpaceCode(error, "version_conflict")
      || isSpaceCode(error, "member_conflict")
      || isSpaceCode(error, "member_order_conflict")
    ) {
      setDialog(null);
      setNotice("Space 已被其他操作更新，已重新加载最新内容");
      refreshDetail();
      refreshList();
    } else if (isSpaceCode(error, "member_not_found")) {
      setDialog(null);
      setNotice("该成员已不在 Space 中，已重新加载最新内容");
      refreshDetail();
      refreshList();
    } else if (isSpaceCode(error, "not_found")) {
      setDialog(null);
      setSelectedSpaceId(null);
      setDetail(null);
      setNotice("该 Space 已不存在，列表已刷新");
      refreshList();
    } else {
      setDetailError(errorMessage(error, fallback));
    }
  };

  const handleMoveMember = async (siteId: string, directionToMove: MemberMove) => {
    if (!detail || memberBusyId) return;
    let snapshot = detail;
    let index = snapshot.members.findIndex((member) => member.site.id === siteId);
    if (index < 0) return;

    setMemberBusyId(siteId);
    setDetailError(null);
    try {
      if (
        directionToMove === "down"
        && index + 2 >= snapshot.members.length
        && snapshot.nextCursor
      ) {
        const expanded = await loadMoreMembers();
        if (!expanded) return;
        snapshot = expanded;
        index = snapshot.members.findIndex((member) => member.site.id === siteId);
      }

      let beforeSiteId: string | null;
      if (directionToMove === "top") {
        beforeSiteId = snapshot.members[0]?.site.id ?? null;
      } else if (directionToMove === "up") {
        beforeSiteId = snapshot.members[index - 1]?.site.id ?? null;
      } else if (directionToMove === "down") {
        if (!snapshot.members[index + 1]) return;
        beforeSiteId = snapshot.members[index + 2]?.site.id ?? null;
      } else {
        beforeSiteId = null;
      }

      await reorderSpaceMembers(snapshot.id, {
        expectedVersion: snapshot.version,
        orderedSiteIds: [siteId],
        beforeSiteId,
      });
      setNotice("Space 成员顺序已更新");
      refreshDetail();
      refreshList();
    } catch (error) {
      handleDetailMutationError(error, "成员顺序更新失败，请重试");
    } finally {
      setMemberBusyId(null);
    }
  };

  const handleRemoveMember = async () => {
    if (dialog?.kind !== "remove" || !detail) return;
    const member = dialog.member;
    const target = detail;
    setMutationBusy(true);
    setMutationError(null);
    try {
      await removeSpaceMember(target.id, member.site.id, target.version);
      setDialog(null);
      setNotice(`已将“${member.site.name}”移出 Space，网站仍保留在资料库中`);
      refreshDetail();
      refreshList();
    } catch (error) {
      if (
        isSpaceCode(error, "version_conflict")
        || isSpaceCode(error, "member_conflict")
      ) {
        handleDetailMutationError(error, "移除成员失败，请重试");
      } else if (isSpaceCode(error, "member_not_found")) {
        setDialog(null);
        setNotice("该成员已不在 Space 中，已重新加载最新内容");
        refreshDetail();
        refreshList();
      } else if (isSpaceCode(error, "not_found")) {
        setDialog(null);
        setSelectedSpaceId(null);
        setDetail(null);
        setNotice("该 Space 已不存在，列表已刷新");
        refreshList();
      } else {
        setMutationError(errorMessage(error, "移除成员失败，请重试"));
      }
    } finally {
      setMutationBusy(false);
    }
  };

  const returnToList = () => {
    setSelectedSpaceId(null);
    setDetail(null);
    setDetailError(null);
    setDialog(null);
    refreshList();
    router.push("/spaces");
  };

  return {
    closeDialog,
    detail,
    detailError,
    detailLoading,
    detailLoadingMore,
    dialog,
    direction,
    handleCreate,
    handleDelete,
    handleMoveMember,
    handleRemoveMember,
    handleRename,
    loadDeletePreview,
    loadMoreMembers,
    loadMoreSpaces,
    memberBusyId,
    mutationBusy,
    mutationError,
    notice,
    openAllTarget,
    openDialog,
    refreshDetail,
    refreshList,
    returnToList,
    selectedSpaceId,
    setDirection,
    setNotice,
    setOpenAllTarget,
    setSelectedSpaceId,
    setSort,
    sort,
    spacePage,
    spacesError,
    spacesLoading,
  };
}
