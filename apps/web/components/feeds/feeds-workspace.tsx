"use client";

import {
  AlertTriangle,
  BookmarkCheck,
  BookmarkPlus,
  Headphones,
  List,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Rss,
  Settings2,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { Spinner } from "@/components/react-bits/spinner";
import { useToast } from "@/components/ui/toast";
import {
  addFeed,
  deleteFeed,
  listFeedItems,
  listFeedPicks,
  listFeeds,
  refreshFeed,
  saveFeedItem,
  setFeedEnabled,
  updateFeedItem,
} from "@/lib/feeds-client";
import {
  feedErrorLabel,
  feedKindLabel,
  formatDuration,
  groupItemsByFeed,
  hostLabel,
  itemMoment,
  type Feed,
  type FeedItem,
  type FeedPick,
} from "@/lib/feeds-contract";
import { siteDetailHref } from "@/lib/quick-add";
import { formatRelativeTime } from "@/lib/relative-time";

type Tab = "picks" | "all" | "manage";

const TABS: ReadonlyArray<{ value: Tab; label: string; icon: typeof Rss }> = [
  { value: "picks", label: "今日精选", icon: Sparkles },
  { value: "all", label: "全部", icon: List },
  { value: "manage", label: "订阅管理", icon: Settings2 },
];

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

type ItemActions = {
  open: (item: FeedItem) => void;
  save: (item: FeedItem) => void;
  dismiss: (item: FeedItem) => void;
  saving: ReadonlySet<string>;
};

function FeedItemCard({
  item,
  source,
  reason,
  now,
  actions,
}: {
  item: FeedItem;
  source?: string;
  reason?: string;
  now: number;
  actions: ItemActions;
}) {
  const duration = formatDuration(item.durationSeconds);
  const moment = formatRelativeTime(itemMoment(item), now);
  const saving = actions.saving.has(item.id);
  return (
    <li className="feed-card" data-read={item.read ? "true" : undefined}>
      <div className="feed-card-main">
        {item.url ? (
          <a
            className="feed-card-title"
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => actions.open(item)}
          >
            {item.title}
          </a>
        ) : (
          <span className="feed-card-title">{item.title}</span>
        )}
        <p className="feed-card-meta">
          {source && <span className="feed-card-source">{source}</span>}
          {moment && <span>{moment}</span>}
          {item.author && <span>{item.author}</span>}
          {duration && (
            <span className="feed-card-duration">
              <Headphones aria-hidden="true" size={12} />
              {duration}
            </span>
          )}
          {item.audioUrl && (
            <a href={item.audioUrl} target="_blank" rel="noopener noreferrer">
              音频
            </a>
          )}
        </p>
        {item.summary && <p className="feed-card-summary">{item.summary}</p>}
        {reason && <p className="feed-card-reason">{reason}</p>}
      </div>
      <div className="feed-card-actions">
        {item.savedSiteId ? (
          <Link className="feed-action" data-tone="saved" href={siteDetailHref(item.savedSiteId)}>
            <BookmarkCheck aria-hidden="true" size={14} />
            已收藏
          </Link>
        ) : (
          <button
            className="feed-action"
            type="button"
            disabled={saving || !item.url}
            onClick={() => actions.save(item)}
            title={item.url ? "收进收藏库" : "这个条目没有链接"}
          >
            {saving ? <Spinner size={14} /> : <BookmarkPlus aria-hidden="true" size={14} />}
            收进收藏库
          </button>
        )}
        <button
          className="feed-action feed-action-icon"
          type="button"
          aria-label={`忽略「${item.title}」`}
          title="忽略"
          onClick={() => actions.dismiss(item)}
        >
          <X aria-hidden="true" size={14} />
        </button>
      </div>
    </li>
  );
}

function FeedManageRow({
  feed,
  now,
  busy,
  onToggle,
  onRefresh,
  onDelete,
}: {
  feed: Feed;
  now: number;
  busy: boolean;
  onToggle: () => void;
  onRefresh: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const failing = feed.lastError !== null;
  return (
    <li className="feed-manage-row">
      <div className="feed-manage-main">
        <span className="feed-manage-title">
          {feed.kind === "podcast" ? (
            <Headphones aria-hidden="true" size={14} />
          ) : (
            <Rss aria-hidden="true" size={14} />
          )}
          {feed.title}
        </span>
        <span className="feed-manage-url">{feed.url}</span>
        <span className="feed-manage-meta">
          <span className="provider-badge">{feedKindLabel(feed.kind)}</span>
          {!feed.enabled && (
            <span className="provider-badge" data-tone="muted">
              已停用
            </span>
          )}
          <span>{feed.itemCount} 条</span>
          {feed.lastFetchedAt && <span>拉取于 {formatRelativeTime(feed.lastFetchedAt, now)}</span>}
        </span>
        {failing && (
          <span className="feed-manage-error" role="status">
            <AlertTriangle aria-hidden="true" size={13} />
            {feedErrorLabel(feed.lastError)}
            {feed.consecutiveFailures > 1 && `（连续 ${feed.consecutiveFailures} 次，已延后重试）`}
          </span>
        )}
      </div>
      <div className="feed-manage-actions">
        {busy && <Spinner size={14} />}
        <button
          className="provider-btn provider-btn-secondary provider-btn-sm"
          type="button"
          disabled={busy || !feed.enabled}
          onClick={onRefresh}
        >
          <RefreshCw aria-hidden="true" size={13} />
          刷新
        </button>
        <button
          className="provider-btn provider-btn-secondary provider-btn-sm"
          type="button"
          disabled={busy}
          onClick={onToggle}
        >
          {feed.enabled ? <Pause aria-hidden="true" size={13} /> : <Play aria-hidden="true" size={13} />}
          {feed.enabled ? "停用" : "启用"}
        </button>
        {confirming ? (
          <>
            <button
              className="provider-btn provider-btn-danger-ghost provider-btn-sm"
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirming(false);
                onDelete();
              }}
            >
              确认删除
            </button>
            <button
              className="provider-btn provider-btn-secondary provider-btn-sm"
              type="button"
              onClick={() => setConfirming(false)}
            >
              取消
            </button>
          </>
        ) : (
          <button
            className="provider-btn provider-btn-danger-ghost provider-btn-sm"
            type="button"
            disabled={busy}
            aria-label={`删除订阅「${feed.title}」`}
            onClick={() => setConfirming(true)}
          >
            <Trash2 aria-hidden="true" size={13} />
          </button>
        )}
      </div>
    </li>
  );
}

export function FeedsWorkspace() {
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("picks");
  const [feeds, setFeeds] = useState<Feed[]>([]);
  const [items, setItems] = useState<FeedItem[]>([]);
  const [picks, setPicks] = useState<FeedPick[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [newUrl, setNewUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [busyFeeds, setBusyFeeds] = useState<ReadonlySet<string>>(new Set());
  const [saving, setSaving] = useState<ReadonlySet<string>>(new Set());
  const [now, setNow] = useState(() => Date.now());

  const [reloadKey, setReloadKey] = useState(0);
  const reload = useCallback(() => setReloadKey((key) => key + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const fetchAll = async () => {
      try {
        const [nextFeeds, nextItems, nextPicks] = await Promise.all([
          listFeeds(signal),
          listFeedItems(signal),
          listFeedPicks(signal),
        ]);
        if (signal.aborted) return;
        setFeeds(nextFeeds);
        setItems(nextItems);
        setPicks(nextPicks);
        setLoadError(null);
        setNow(Date.now());
      } catch (error) {
        if (signal.aborted) return;
        setLoadError(errorText(error, "订阅加载失败，请稍后重试"));
      } finally {
        if (!signal.aborted) setLoading(false);
      }
    };
    void fetchAll();
    return () => controller.abort();
  }, [reloadKey]);

  const feedTitles = useMemo(() => new Map(feeds.map((feed) => [feed.id, feed.title])), [feeds]);
  const groups = useMemo(() => groupItemsByFeed(feeds, items), [feeds, items]);
  const visiblePicks = useMemo(() => picks.filter((pick) => !pick.item.dismissed), [picks]);

  const patchItem = useCallback((next: FeedItem) => {
    setItems((current) => current.map((item) => (item.id === next.id ? next : item)));
    setPicks((current) =>
      current.map((pick) => (pick.item.id === next.id ? { ...pick, item: next } : pick)),
    );
  }, []);

  const open = useCallback(
    (item: FeedItem) => {
      if (item.read) return;
      patchItem({ ...item, read: true });
      void updateFeedItem(item.id, { read: true }).catch(() => patchItem(item));
    },
    [patchItem],
  );

  const dismiss = useCallback(
    (item: FeedItem) => {
      // 可逆操作：乐观隐藏 + 5 秒撤销（2026-10 D6）。
      patchItem({ ...item, dismissed: true });
      void updateFeedItem(item.id, { dismissed: true })
        .then(() =>
          toast.show({
            message: "已忽略这一条",
            action: {
              label: "撤销",
              onAction: () => {
                patchItem({ ...item, dismissed: false });
                void updateFeedItem(item.id, { dismissed: false }).catch(() => {
                  patchItem({ ...item, dismissed: true });
                  toast.show({ message: "撤销没有成功，请稍后重试", tone: "danger" });
                });
              },
            },
          }),
        )
        .catch((error: unknown) => {
          patchItem(item);
          toast.show({ message: errorText(error, "忽略失败，请稍后重试"), tone: "danger" });
        });
    },
    [patchItem, toast],
  );

  const save = useCallback(
    (item: FeedItem) => {
      setSaving((current) => new Set(current).add(item.id));
      void saveFeedItem(item.id)
        .then((result) => {
          patchItem(result.item);
          toast.show({
            message: result.created ? "已收进收藏库" : "收藏库里已有这个链接，已关联",
            link: { label: "查看", href: siteDetailHref(result.siteId) },
          });
        })
        .catch((error: unknown) => {
          toast.show({ message: errorText(error, "收藏失败，请稍后重试"), tone: "danger" });
        })
        .finally(() =>
          setSaving((current) => {
            const next = new Set(current);
            next.delete(item.id);
            return next;
          }),
        );
    },
    [patchItem, toast],
  );

  const actions: ItemActions = { open, save, dismiss, saving };

  const withFeedBusy = async (feedId: string, run: () => Promise<void>) => {
    setBusyFeeds((current) => new Set(current).add(feedId));
    try {
      await run();
    } catch (error) {
      toast.show({ message: errorText(error, "操作失败，请稍后重试"), tone: "danger" });
    } finally {
      setBusyFeeds((current) => {
        const next = new Set(current);
        next.delete(feedId);
        return next;
      });
    }
  };

  const replaceFeed = (next: Feed) =>
    setFeeds((current) => current.map((feed) => (feed.id === next.id ? next : feed)));

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!newUrl.trim() || adding) return;
    setAdding(true);
    setAddError(null);
    try {
      const feed = await addFeed(newUrl);
      setNewUrl("");
      toast.show({
        message: feed.lastError
          ? `已添加，但首次拉取失败：${feedErrorLabel(feed.lastError)}`
          : `已订阅「${feed.title}」，拉到 ${feed.itemCount} 条`,
        tone: feed.lastError ? "danger" : "neutral",
      });
      reload();
    } catch (error) {
      setAddError(errorText(error, "添加失败，请检查地址"));
    } finally {
      setAdding(false);
    }
  };

  return (
    <main className="site-main">
      <header className="workspace-page-header">
        <span className="page-kicker">订阅</span>
        <h1>订阅流</h1>
        <p className="provider-page-lead">
          RSS 与播客的新内容只在这里浏览，30 天后自动清理；觉得值得留下的，一键收进收藏库。
          “今日精选”按你收藏库里的常用标签与分类挑选，同一来源最多 3 条。
        </p>
      </header>

      <div className="feeds-tabs" role="tablist" aria-label="订阅视图">
        {TABS.map(({ value, label, icon: Icon }) => (
          <button
            key={value}
            className="feeds-tab"
            type="button"
            role="tab"
            aria-selected={tab === value}
            onClick={() => setTab(value)}
          >
            <Icon aria-hidden="true" size={14} />
            {label}
          </button>
        ))}
      </div>

      {loadError && (
        <p className="provider-error" role="alert">
          <span>{loadError}</span>
          <button
            className="provider-btn provider-btn-secondary provider-btn-sm"
            type="button"
            onClick={() => {
              setLoading(true);
              reload();
            }}
          >
            <RefreshCw aria-hidden="true" size={13} />
            重试
          </button>
        </p>
      )}

      {loading ? (
        <p className="provider-loading" aria-busy="true">
          <Spinner size={14} /> 正在加载订阅…
        </p>
      ) : loadError && !feeds.length ? null : (
        <div className="feeds-panel" role="tabpanel">
          {tab === "picks" &&
            (visiblePicks.length ? (
              <ol className="feed-list">
                {visiblePicks.map((pick) => (
                  <FeedItemCard
                    key={pick.item.id}
                    item={pick.item}
                    source={pick.feedTitle}
                    reason={pick.reason}
                    now={now}
                    actions={actions}
                  />
                ))}
              </ol>
            ) : (
              <p className="provider-empty">
                {feeds.length
                  ? "最近 7 天还没有新内容。"
                  : "还没有订阅。到“订阅管理”添加一个 RSS 或播客地址。"}
              </p>
            ))}

          {tab === "all" &&
            (groups.length ? (
              groups.map(({ feed, items: feedItems }) => (
                <section className="feed-group" key={feed.id} aria-label={feed.title}>
                  <h2 className="feed-group-title">
                    {feed.kind === "podcast" ? (
                      <Headphones aria-hidden="true" size={14} />
                    ) : (
                      <Rss aria-hidden="true" size={14} />
                    )}
                    {feed.title}
                    <span className="feed-group-count">{feedItems.length}</span>
                    {feed.siteUrl && <span className="feed-group-host">{hostLabel(feed.siteUrl)}</span>}
                  </h2>
                  <ul className="feed-list">
                    {feedItems.map((item) => (
                      <FeedItemCard key={item.id} item={item} now={now} actions={actions} />
                    ))}
                  </ul>
                </section>
              ))
            ) : (
              <p className="provider-empty">还没有条目。</p>
            ))}

          {tab === "manage" && (
            <section className="provider-section" aria-labelledby="feeds-manage-title">
              <h2 className="provider-section-title" id="feeds-manage-title">
                订阅源
              </h2>
              <form className="feeds-add" onSubmit={submit}>
                <input
                  className="provider-input"
                  type="url"
                  inputMode="url"
                  placeholder="https://example.com/feed.xml"
                  aria-label="RSS / 播客订阅地址"
                  value={newUrl}
                  maxLength={2048}
                  onChange={(event) => setNewUrl(event.target.value)}
                />
                <button
                  className="provider-btn provider-btn-primary"
                  type="submit"
                  disabled={adding || !newUrl.trim()}
                >
                  {adding ? <Spinner size={14} /> : <Plus aria-hidden="true" size={14} />}
                  添加
                </button>
              </form>
              {addError && (
                <p className="provider-error" role="alert">
                  {addError}
                </p>
              )}
              <p className="provider-form-hint">
                多数中文站点没有 RSS，可在同一台服务器自部署 RSSHub 后订阅它生成的地址（见 docs/feeds-rsshub.md）。
              </p>
              {feeds.length ? (
                <ul className="feed-manage-list">
                  {feeds.map((feed) => (
                    <FeedManageRow
                      key={feed.id}
                      feed={feed}
                      now={now}
                      busy={busyFeeds.has(feed.id)}
                      onToggle={() =>
                        void withFeedBusy(feed.id, async () =>
                          replaceFeed(await setFeedEnabled(feed.id, !feed.enabled)),
                        )
                      }
                      onRefresh={() =>
                        void withFeedBusy(feed.id, async () => {
                          replaceFeed(await refreshFeed(feed.id));
                          reload();
                        })
                      }
                      onDelete={() =>
                        void withFeedBusy(feed.id, async () => {
                          await deleteFeed(feed.id);
                          reload();
                        })
                      }
                    />
                  ))}
                </ul>
              ) : (
                <p className="provider-empty">还没有订阅源。</p>
              )}
            </section>
          )}
        </div>
      )}
      {/* 供屏幕阅读器了解精选数量；视觉上由列表本身表达。 */}
      <p className="sr-only" aria-live="polite">
        {tab === "picks" && !loading ? `今日精选 ${visiblePicks.length} 条，来自 ${feedTitles.size} 个订阅源` : ""}
      </p>
    </main>
  );
}
