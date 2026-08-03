"use client";

import { Check, ChevronDown, Search } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

export type ThemedSelectOption<T extends string> = {
  value: T;
  label: string;
  disabled?: boolean;
  keywords?: readonly string[];
};

export type ThemedSelectProps<T extends string> = {
  value: T;
  options: readonly ThemedSelectOption<T>[];
  onValueChange: (value: T) => void;
  id?: string;
  ariaLabel?: string;
  labelledBy?: string;
  disabled?: boolean;
  invalid?: boolean;
  variant?: "field" | "toolbar" | "plain";
  searchable?: boolean;
  align?: "start" | "end";
  className?: string;
};

type OpenOptions = {
  active?: "selected" | "first" | "last";
  initialQuery?: string;
};

const MENU_GAP = 6;
const VIEWPORT_MARGIN = 8;
const MENU_MAX_HEIGHT = 288;
const TYPEAHEAD_RESET_MS = 600;
const FOCUSABLE_SELECTOR = [
  'a[href]:not([tabindex="-1"])',
  'button:not([disabled]):not([tabindex="-1"])',
  'input:not([disabled]):not([type="hidden"]):not([tabindex="-1"])',
  'textarea:not([disabled]):not([tabindex="-1"])',
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function normalizeSearchText(value: string) {
  return value.trim().toLocaleLowerCase();
}

function optionMatches<T extends string>(option: ThemedSelectOption<T>, query: string) {
  const normalizedQuery = normalizeSearchText(query);
  if (!normalizedQuery) return true;

  return [option.label, option.value, ...(option.keywords ?? [])]
    .some((candidate) => normalizeSearchText(candidate).includes(normalizedQuery));
}

function firstEnabledIndex<T extends string>(
  options: readonly ThemedSelectOption<T>[],
  fromEnd = false,
) {
  if (fromEnd) {
    for (let index = options.length - 1; index >= 0; index -= 1) {
      if (!options[index]?.disabled) return index;
    }
    return -1;
  }

  return options.findIndex((option) => !option.disabled);
}

function enabledIndexFrom<T extends string>(
  options: readonly ThemedSelectOption<T>[],
  currentIndex: number,
  direction: 1 | -1,
) {
  if (options.length === 0) return -1;

  for (let offset = 1; offset <= options.length; offset += 1) {
    const index = (currentIndex + direction * offset + options.length) % options.length;
    if (!options[index]?.disabled) return index;
  }

  return -1;
}

function joinClassNames(...values: Array<string | undefined | false>) {
  return values.filter(Boolean).join(" ");
}

export function ThemedSelect<T extends string>({
  value,
  options,
  onValueChange,
  id,
  ariaLabel,
  labelledBy,
  disabled = false,
  invalid = false,
  variant = "field",
  searchable = false,
  align = "start",
  className,
}: Readonly<ThemedSelectProps<T>>) {
  const generatedId = useId();
  const controlId = id ?? `themed-select-${generatedId}`;
  const listboxId = `${controlId}-listbox`;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const typeaheadRef = useRef("");
  const typeaheadTimerRef = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const [portalTarget, setPortalTarget] = useState<HTMLElement | null>(null);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);

  const selectedOption = useMemo(
    () => options.find((option) => option.value === value),
    [options, value],
  );
  const filteredOptions = useMemo(
    () => options.filter((option) => optionMatches(option, query)),
    [options, query],
  );

  const selectedFilteredIndex = filteredOptions.findIndex(
    (option) => option.value === value && !option.disabled,
  );
  const resolvedActiveIndex = filteredOptions[activeIndex] && !filteredOptions[activeIndex]?.disabled
    ? activeIndex
    : selectedFilteredIndex >= 0
      ? selectedFilteredIndex
      : firstEnabledIndex(filteredOptions);
  const activeDescendant = open && resolvedActiveIndex >= 0
    ? `${listboxId}-option-${resolvedActiveIndex}`
    : undefined;

  const clearTypeahead = useCallback(() => {
    typeaheadRef.current = "";
    if (typeaheadTimerRef.current !== null) {
      window.clearTimeout(typeaheadTimerRef.current);
      typeaheadTimerRef.current = null;
    }
  }, []);

  const closeMenu = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    setPortalTarget(null);
    setQuery("");
    clearTypeahead();

    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  }, [clearTypeahead]);

  const focusAdjacentControl = useCallback((backward: boolean) => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const scope = trigger.closest('dialog, [role="dialog"]') ?? document;
    const popup = popupRef.current;
    const focusable = Array.from(scope.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter((element) => (
        !popup?.contains(element) &&
        !element.closest("[inert]") &&
        element.getAttribute("aria-hidden") !== "true" &&
        element.getClientRects().length > 0
      ));
    const triggerIndex = focusable.indexOf(trigger);
    if (triggerIndex < 0 || focusable.length < 2) {
      trigger.focus();
      return;
    }
    const offset = backward ? -1 : 1;
    const nextIndex = (triggerIndex + offset + focusable.length) % focusable.length;
    focusable[nextIndex]?.focus();
  }, []);

  const getOpeningIndex = useCallback((
    sourceOptions: readonly ThemedSelectOption<T>[],
    preference: OpenOptions["active"],
  ) => {
    if (preference === "first") return firstEnabledIndex(sourceOptions);
    if (preference === "last") return firstEnabledIndex(sourceOptions, true);

    const selectedIndex = sourceOptions.findIndex(
      (option) => option.value === value && !option.disabled,
    );
    return selectedIndex >= 0 ? selectedIndex : firstEnabledIndex(sourceOptions);
  }, [value]);

  const openMenu = useCallback((openOptions: OpenOptions = {}) => {
    if (disabled || typeof document === "undefined") return;

    const initialQuery = searchable ? (openOptions.initialQuery ?? "") : "";
    const visibleOptions = initialQuery
      ? options.filter((option) => optionMatches(option, initialQuery))
      : options;
    const dialog = triggerRef.current?.closest("dialog");

    setPortalTarget(dialog ?? document.body);
    setQuery(initialQuery);
    setActiveIndex(getOpeningIndex(visibleOptions, openOptions.active));
    setOpen(true);
  }, [disabled, getOpeningIndex, options, searchable]);

  const selectOption = useCallback((option: ThemedSelectOption<T> | undefined) => {
    if (disabled || !option || option.disabled) return;
    if (option.value !== value) onValueChange(option.value);
    closeMenu(true);
  }, [closeMenu, disabled, onValueChange, value]);

  const moveActive = useCallback((direction: 1 | -1) => {
    const currentIndex = resolvedActiveIndex >= 0
      ? resolvedActiveIndex
      : direction === 1
        ? -1
        : 0;
    setActiveIndex(enabledIndexFrom(filteredOptions, currentIndex, direction));
  }, [filteredOptions, resolvedActiveIndex]);

  const moveToEdge = useCallback((edge: "first" | "last") => {
    setActiveIndex(firstEnabledIndex(filteredOptions, edge === "last"));
  }, [filteredOptions]);

  const runTypeahead = useCallback((character: string) => {
    const previousQuery = typeaheadRef.current;
    const combinedQuery = `${previousQuery}${character}`.toLocaleLowerCase();

    const findMatch = (searchValue: string) => {
      if (!searchValue) return -1;
      const start = Math.max(resolvedActiveIndex, -1);
      for (let offset = 1; offset <= options.length; offset += 1) {
        const index = (start + offset) % options.length;
        const option = options[index];
        if (!option || option.disabled) continue;
        const candidates = [option.label, option.value, ...(option.keywords ?? [])];
        if (candidates.some((candidate) => normalizeSearchText(candidate).startsWith(searchValue))) {
          return index;
        }
      }
      return -1;
    };

    let matchIndex = findMatch(combinedQuery);
    let nextQuery = combinedQuery;
    if (matchIndex < 0 && previousQuery) {
      nextQuery = character.toLocaleLowerCase();
      matchIndex = findMatch(nextQuery);
    }

    typeaheadRef.current = nextQuery;
    if (typeaheadTimerRef.current !== null) window.clearTimeout(typeaheadTimerRef.current);
    typeaheadTimerRef.current = window.setTimeout(clearTypeahead, TYPEAHEAD_RESET_MS);

    if (matchIndex >= 0) {
      if (!open) openMenu();
      setActiveIndex(matchIndex);
    }
  }, [clearTypeahead, open, openMenu, options, resolvedActiveIndex]);

  const handleTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;

    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (open) moveActive(1);
        else openMenu();
        return;
      case "ArrowUp":
        event.preventDefault();
        if (open) moveActive(-1);
        else openMenu();
        return;
      case "Home":
        event.preventDefault();
        if (open) moveToEdge("first");
        else openMenu({ active: "first" });
        return;
      case "End":
        event.preventDefault();
        if (open) moveToEdge("last");
        else openMenu({ active: "last" });
        return;
      case "Enter":
      case " ":
        event.preventDefault();
        if (open) selectOption(filteredOptions[resolvedActiveIndex]);
        else openMenu();
        return;
      default:
        break;
    }

    if (event.key.length !== 1) return;
    event.preventDefault();
    if (searchable) {
      openMenu({ initialQuery: event.key });
    } else {
      runTypeahead(event.key);
    }
  };

  const handleSearchKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(1);
        break;
      case "ArrowUp":
        event.preventDefault();
        moveActive(-1);
        break;
      case "Home":
        event.preventDefault();
        moveToEdge("first");
        break;
      case "End":
        event.preventDefault();
        moveToEdge("last");
        break;
      case "Enter":
        event.preventDefault();
        selectOption(filteredOptions[resolvedActiveIndex]);
        break;
      default:
        break;
    }
  };

  useLayoutEffect(() => {
    if (!open || !portalTarget) return;

    const trigger = triggerRef.current;
    const popup = popupRef.current;
    if (!trigger || !popup) return;

    const updatePosition = () => {
      const triggerRect = trigger.getBoundingClientRect();
      const visualViewport = window.visualViewport;
      const viewportLeft = visualViewport?.offsetLeft ?? 0;
      const viewportTop = visualViewport?.offsetTop ?? 0;
      const viewportWidth = visualViewport?.width ?? document.documentElement.clientWidth;
      const viewportHeight = visualViewport?.height ?? document.documentElement.clientHeight;
      const viewportRight = viewportLeft + viewportWidth;
      const viewportBottom = viewportTop + viewportHeight;
      const availableWidth = Math.max(0, viewportWidth - VIEWPORT_MARGIN * 2);
      const preferredWidth = Math.max(triggerRect.width, searchable ? 280 : 200);
      const popupWidth = Math.min(preferredWidth, availableWidth);

      popup.style.width = `${popupWidth}px`;
      popup.style.maxHeight = `${MENU_MAX_HEIGHT}px`;

      const naturalHeight = Math.min(popup.scrollHeight, MENU_MAX_HEIGHT);
      const spaceBelow = viewportBottom - triggerRect.bottom - MENU_GAP - VIEWPORT_MARGIN;
      const spaceAbove = triggerRect.top - viewportTop - MENU_GAP - VIEWPORT_MARGIN;
      const openAbove = spaceBelow < Math.min(naturalHeight, 160) && spaceAbove > spaceBelow;
      const availableHeight = Math.max(40, openAbove ? spaceAbove : spaceBelow);
      const popupMaxHeight = Math.min(MENU_MAX_HEIGHT, availableHeight);
      const renderedHeight = Math.min(naturalHeight, popupMaxHeight);
      const unclampedTop = openAbove
        ? triggerRect.top - MENU_GAP - renderedHeight
        : triggerRect.bottom + MENU_GAP;
      const preferredLeft = align === "end"
        ? triggerRect.right - popupWidth
        : triggerRect.left;
      const minLeft = viewportLeft + VIEWPORT_MARGIN;
      const maxLeft = Math.max(minLeft, viewportRight - popupWidth - VIEWPORT_MARGIN);
      const left = Math.min(Math.max(preferredLeft, minLeft), maxLeft);
      const minTop = viewportTop + VIEWPORT_MARGIN;
      const maxTop = Math.max(minTop, viewportBottom - renderedHeight - VIEWPORT_MARGIN);
      const top = Math.min(Math.max(unclampedTop, minTop), maxTop);

      popup.style.left = `${Math.round(left)}px`;
      popup.style.top = `${Math.round(top)}px`;
      popup.style.maxHeight = `${Math.floor(popupMaxHeight)}px`;
      popup.dataset.positioned = "true";
      popup.dataset.placement = openAbove ? "top" : "bottom";
    };

    let scheduledFrame: number | null = null;
    const schedulePosition = () => {
      if (scheduledFrame !== null) return;
      scheduledFrame = window.requestAnimationFrame(() => {
        scheduledFrame = null;
        updatePosition();
      });
    };

    updatePosition();
    schedulePosition();
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(schedulePosition);
    resizeObserver?.observe(trigger);
    resizeObserver?.observe(popup);
    window.addEventListener("resize", schedulePosition);
    document.addEventListener("scroll", schedulePosition, true);
    window.visualViewport?.addEventListener("resize", schedulePosition);
    window.visualViewport?.addEventListener("scroll", schedulePosition);

    return () => {
      if (scheduledFrame !== null) window.cancelAnimationFrame(scheduledFrame);
      resizeObserver?.disconnect();
      window.removeEventListener("resize", schedulePosition);
      document.removeEventListener("scroll", schedulePosition, true);
      window.visualViewport?.removeEventListener("resize", schedulePosition);
      window.visualViewport?.removeEventListener("scroll", schedulePosition);
    };
  }, [align, open, portalTarget, searchable]);

  useLayoutEffect(() => {
    if (!open) return;
    if (searchable) searchInputRef.current?.focus();
  }, [open, searchable]);

  useLayoutEffect(() => {
    if (!open || resolvedActiveIndex < 0) return;
    optionRefs.current[resolvedActiveIndex]?.scrollIntoView({ block: "nearest" });
  }, [open, resolvedActiveIndex]);

  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (triggerRef.current?.contains(target) || popupRef.current?.contains(target)) return;
      closeMenu(false);
    };
    const handleDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeMenu(true);
      } else if (event.key === "Tab") {
        event.preventDefault();
        event.stopPropagation();
        focusAdjacentControl(event.shiftKey);
        closeMenu(false);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleDocumentKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleDocumentKeyDown, true);
    };
  }, [closeMenu, focusAdjacentControl, open]);

  useEffect(() => {
    if (!disabled || !open) return;
    const frame = window.requestAnimationFrame(() => closeMenu(false));
    return () => window.cancelAnimationFrame(frame);
  }, [closeMenu, disabled, open]);

  useEffect(() => () => {
    if (typeaheadTimerRef.current !== null) {
      window.clearTimeout(typeaheadTimerRef.current);
    }
  }, []);

  const popup = open && portalTarget ? (
    <div
      ref={popupRef}
      className="themed-select-popover"
      data-searchable={searchable || undefined}
    >
      {searchable ? (
        <label className="themed-select-search">
          <Search aria-hidden="true" />
          <span className="sr-only">搜索选项</span>
          <input
            ref={searchInputRef}
            type="search"
            value={query}
            onChange={(event) => {
              const nextQuery = event.target.value;
              const nextOptions = options.filter((option) => optionMatches(option, nextQuery));
              setQuery(nextQuery);
              setActiveIndex(firstEnabledIndex(nextOptions));
            }}
            onKeyDown={handleSearchKeyDown}
            placeholder="搜索选项"
            autoComplete="off"
            role="searchbox"
            aria-controls={listboxId}
            aria-activedescendant={activeDescendant}
          />
        </label>
      ) : null}

      <div
        id={listboxId}
        className="themed-select-listbox"
        role="listbox"
        aria-label={labelledBy ? undefined : (ariaLabel ?? "选择选项")}
        aria-labelledby={labelledBy}
      >
        {filteredOptions.length > 0 ? filteredOptions.map((option, index) => {
          const selected = option.value === value;
          const active = index === resolvedActiveIndex;
          return (
            <button
              key={`${option.value}-${index}`}
              ref={(element) => {
                optionRefs.current[index] = element;
              }}
              id={`${listboxId}-option-${index}`}
              className="themed-select-option"
              type="button"
              role="option"
              tabIndex={-1}
              disabled={option.disabled}
              aria-disabled={option.disabled || undefined}
              aria-selected={selected}
              data-active={active || undefined}
              data-selected={selected || undefined}
              onPointerDown={(event) => event.preventDefault()}
              onPointerMove={() => {
                if (!option.disabled) setActiveIndex(index);
              }}
              onClick={() => selectOption(option)}
            >
              <span className="themed-select-option-label">{option.label}</span>
              <span className="themed-select-option-check" data-visible={selected || undefined}>
                <Check aria-hidden="true" />
              </span>
            </button>
          );
        }) : (
          <div className="themed-select-empty" role="status">没有匹配的选项</div>
        )}
      </div>
    </div>
  ) : null;

  return (
    <div
      className={joinClassNames("themed-select", className)}
      data-variant={variant}
      data-open={open || undefined}
      data-disabled={disabled || undefined}
      data-invalid={invalid || undefined}
    >
      <button
        ref={triggerRef}
        id={controlId}
        className="themed-select-trigger"
        type="button"
        role="combobox"
        disabled={disabled}
        aria-label={ariaLabel}
        aria-labelledby={labelledBy}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={!searchable ? activeDescendant : undefined}
        aria-invalid={invalid || undefined}
        onClick={() => {
          if (open) closeMenu(false);
          else openMenu();
        }}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="themed-select-value">{(selectedOption?.label ?? value) || "请选择"}</span>
        <ChevronDown className="themed-select-chevron" aria-hidden="true" />
      </button>
      {popup && portalTarget ? createPortal(popup, portalTarget) : null}
    </div>
  );
}
