"use client";

import { Globe2 } from "lucide-react";
import { useState } from "react";

type SiteFaviconProps = {
  url: string | null;
  name: string;
  size?: "small" | "medium" | "large";
};

function SiteFaviconContent({ url, name }: Readonly<Omit<SiteFaviconProps, "size">>) {
  const [failed, setFailed] = useState(false);

  return (
    <>
      {url && !failed ? (
        // Arbitrary account-owned favicon hosts cannot be represented by a static Next image allowlist.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
      ) : name.trim() ? (
        <span>{Array.from(name.trim())[0]?.toLocaleUpperCase()}</span>
      ) : (
        <Globe2 />
      )}
    </>
  );
}

export function SiteFavicon({ url, name, size = "medium" }: Readonly<SiteFaviconProps>) {
  return (
    <span className="site-favicon" data-size={size} aria-hidden="true">
      <SiteFaviconContent key={url ?? ""} url={url} name={name} />
    </span>
  );
}
