import React, { useEffect, useState } from "react";
import type { Publication } from "./app";
import "./external-app.css";

const api = "/lifedata-ai/api/publications/";

export type ExternalAppConfiguration = { preset: "external-app"; url: string };

function configuration(
  publication?: Publication,
): ExternalAppConfiguration | undefined {
  const value = publication?.configuration;
  return value && value.preset === "external-app"
    ? (value as ExternalAppConfiguration)
    : undefined;
}

function readyUrl(value?: string): string {
  if (!value) return "";
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.toString() : "";
  } catch {
    return "";
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      body.error || body.message || `Request failed (${response.status})`,
    );
  }
  return response.json();
}

function ExternalForm({
  publication,
  onClose,
  onPublished,
}: {
  publication?: Publication;
  onClose: () => void;
  onPublished: (url: string) => void;
}) {
  const [title, setTitle] = useState(publication?.title || "");
  const [url, setUrl] = useState(configuration(publication)?.url || "");
  const [theme, setTheme] = useState<"light" | "dark">(
    publication?.theme === "dark" ? "dark" : "light",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  function save() {
    if (busy) return;
    setBusy(true);
    setError("");
    request<Publication>(`${api}external/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        publication
          ? {
              publication_id: publication.id,
              title: title.trim(),
              url: url.trim(),
              theme,
            }
          : { title: title.trim(), url: url.trim(), theme },
      ),
    })
      .then((result) => onPublished(result.url))
      .catch((err) => {
        setError(err.message);
        setBusy(false);
      });
  }
  function remove() {
    if (!publication) return;
    if (!window.confirm("Delete this external application from the library?"))
      return;
    setBusy(true);
    setError("");
    fetch(`${api}${publication.id}/delete/`, {
      method: "DELETE",
      credentials: "same-origin",
    })
      .then((response) => {
        if (!response.ok)
          return response
            .json()
            .then((body) => {
              throw new Error(body.error || "Delete failed");
            });
        location.href = "/lifedata-ai/dashboards/";
      })
      .catch((err) => {
        setError(err.message);
        setBusy(false);
      });
  }
  const valid = title.trim() !== "" && url.trim() !== "";
  return (
    <div className="creator-overlay" onClick={onClose}>
      <aside
        className="creator-panel"
        role="dialog"
        aria-modal="true"
        aria-label={
          publication ? "Edit external application" : "Add external application"
        }
        onClick={(event) => event.stopPropagation()}
      >
        <header className="creator-head">
          <h2>
            {publication ? "Edit external application" : "Add external application"}
          </h2>
          <button
            className="creator-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>
        <div className="creator-form">
          <label className="creator-field">
            Title
            <input
              value={title}
              maxLength={255}
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>
          <label className="creator-field">
            App URL
            <input
              value={url}
              placeholder="https://…"
              onChange={(event) => setUrl(event.target.value)}
            />
          </label>
          <div className="creator-field">
            Theme
            <div className="theme-toggle">
              <button
                type="button"
                className={theme === "light" ? "active" : ""}
                onClick={() => setTheme("light")}
              >
                Light
              </button>
              <button
                type="button"
                className={theme === "dark" ? "active" : ""}
                onClick={() => setTheme("dark")}
              >
                Dark
              </button>
            </div>
          </div>
          <p className="creator-note">
            Only https addresses on approved hosts can be embedded.
          </p>
          {error && <div className="error">{error}</div>}
          <button className="home-cta" disabled={busy || !valid} onClick={save}>
            {busy ? "Saving…" : publication ? "Save changes" : "Add to library"}
          </button>
          {publication && (
            <button className="creator-back" disabled={busy} onClick={remove}>
              Delete application
            </button>
          )}
        </div>
      </aside>
    </div>
  );
}

export function ExternalAppCreator({
  onClose,
  onPublished,
}: {
  onClose: () => void;
  onPublished: (url: string) => void;
}) {
  return <ExternalForm onClose={onClose} onPublished={onPublished} />;
}

export default function ExternalApp({
  publication,
  theme,
}: {
  publication: Publication;
  theme: "light" | "dark";
}) {
  const [editing, setEditing] = useState(false);
  const config = configuration(publication);
  const url = readyUrl(config?.url);
  return (
    <main className={`external-app-shell ${theme}`}>
      {url ? (
        <iframe
          className="external-app-frame"
          src={url}
          title={publication.title}
          sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-popups"
          referrerPolicy="strict-origin-when-cross-origin"
          allow="fullscreen"
          loading="eager"
        />
      ) : (
        <div className="external-app-missing" role="alert">
          This external application URL is missing or not a valid https
          address.
        </div>
      )}
      {publication.can_edit && (
        <button
          type="button"
          className="external-app-edit"
          title="Edit external application"
          aria-label="Edit external application"
          onClick={() => setEditing(true)}
        >
          ⚙ Edit
        </button>
      )}
      {editing && (
        <ExternalForm
          publication={publication}
          onClose={() => setEditing(false)}
          onPublished={(next) => {
            location.href = next;
          }}
        />
      )}
    </main>
  );
}
