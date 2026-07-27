import type { LibrarySite, LibrarySiteUpdateInput } from "./library-contract.ts";

export type LibrarySiteFormValues = {
  name: string;
  url: string;
  description: string;
  faviconUrl: string;
  categoryId: string;
  tagIds: string[];
  pinned: boolean;
};

function sameIds(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false;
  const expected = new Set(right);
  return left.every((id) => expected.has(id));
}

/** Build a PATCH from fields the user actually changed, not a stale form snapshot. */
export function buildLibrarySiteUpdate(
  site: LibrarySite,
  values: LibrarySiteFormValues,
): LibrarySiteUpdateInput | null {
  const input: LibrarySiteUpdateInput = { expectedVersion: site.version };
  let changed = false;

  const name = values.name.trim();
  if (name !== site.name) {
    input.name = name;
    changed = true;
  }

  const url = values.url.trim();
  if (url !== site.originalUrl) {
    input.url = url;
    changed = true;
  }

  const description = values.description.trim() || null;
  if (description !== (site.description?.trim() || null)) {
    input.description = description;
    changed = true;
  }

  const faviconUrl = values.faviconUrl.trim() || null;
  if (faviconUrl !== site.faviconUrl) {
    input.faviconUrl = faviconUrl;
    changed = true;
  }

  const categoryId = values.categoryId || null;
  if (categoryId !== site.category.id) {
    input.categoryId = categoryId;
    changed = true;
  }

  if (!sameIds(values.tagIds, site.tags.map((tag) => tag.id))) {
    input.tagIds = values.tagIds;
    changed = true;
  }

  if (values.pinned !== site.pinned) {
    input.pinned = values.pinned;
    changed = true;
  }

  return changed ? input : null;
}
