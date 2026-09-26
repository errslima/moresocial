# Execution plan: left sidebar and WhatsApp conversations

Implement the following changes in `moresocial/`. All file paths below are relative to that directory.

The existing WhatsApp imports provide the required data; no new integration is needed. The scope is **viewing conversations only**, consistent with Moresocial's current read-only behavior. Keep the existing branding and light/dark themes; use EnzoSocial as the reference for layout.

## 1. Understand the existing implementation

Read these files first:

| File | Purpose |
|---|---|
| `app/templates/base.html` | Shared layout, navigation, logout |
| `app/static/app.css` | Styling and light/dark variables |
| `app/static/app.js` | Progressive enhancement |
| `app/pages.py` | Existing authenticated pages and source browsing |
| `app/web.py` | Authentication, rendering, router registration |
| `app/models.py` | `Source`, `Person`, `PersonIdentifier` |
| `app/repo.py` | Workspace-scoped database access |
| `app/whatsapp.py` | Imported message fields and connector behavior |
| `tests/test_browser.py` | Browser fixtures and existing workflows |
| `tests/test_whatsapp.py` | WhatsApp fixtures and isolation tests |

Use FastAPI, SQLAlchemy, Jinja and the existing CSS/JavaScript. Preserve unrelated work in the checkout.

## 2. Replace the top navigation with a shared left sidebar

Update `base.html` and `app.css`.

On desktop, authenticated pages should have a full-height sidebar approximately **240px wide**, with the content filling the remaining width.

Sidebar contents, in order:

- Moresocial brand at the top.
- Home, WhatsApp, Suggestions, People, Ask, Gatherings, About me, Connections.
- Admin, visible only under the existing administrator condition.
- Sign out anchored at the bottom.

Use vertically stacked links with comfortable padding, optional small inline SVG icons, and a rounded highlighted background plus left accent for the active item. Preserve `aria-current="page"`.

Keep logout as the existing POST form with its CSRF token.

Introduce explicit layout classes or Jinja blocks so ordinary pages retain their approximately 860px reading width, centered **inside the content area**. WhatsApp must be able to fill the available content width and height.

Keep the public welcome page in a simple layout without the authenticated sidebar. Preserve the skip link, keyboard focus styles, notices and read-only footer. Position the footer so it does not interfere with the WhatsApp panes.

At widths below approximately **900px**, use a compact brand/menu header that expands a vertical navigation panel. Prefer native `<details>/<summary>` so navigation works without JavaScript. Avoid rendering duplicate navigation links.

## 3. Add a dedicated WhatsApp page

Create:

- `app/whatsapp_pages.py` — authenticated page route.
- `app/conversations.py` — query and presentation helpers.
- `app/templates/whatsapp.html` — conversation browser.

Register the new router in `app/web.py`.

Use `GET /whatsapp`, which becomes `/moresocial/whatsapp` through the existing prefix configuration. Use the sidebar label and page title **WhatsApp**.

Use URL query parameters for selection and search:

```text
/whatsapp
/whatsapp?q=Sam
/whatsapp?account=<encoded-provider-account>&chat=<encoded-conversation-id>
```

Generate URLs with the existing `url()` helper and proper query-string encoding. Never concatenate unescaped query values.

Implement the page with server-rendered links and GET forms. Refresh, browser Back and direct links must work without client-side routing.

Desktop layout:

```text
┌────────────────┬────────────────────────┬──────────────────────────┐
│ App sidebar    │ WhatsApp               │ Selected conversation    │
│                │ Search conversations   │                          │
│ Home           ├────────────────────────┤ Message history          │
│ WhatsApp       │ Avatar  Name     Time  │                          │
│ Suggestions    │         Last message   │                          │
│ …              │                        │                          │
│                │ More conversations     │                          │
│ Sign out       │                        │                          │
└────────────────┴────────────────────────┴──────────────────────────┘
```

Make the conversation list approximately **340px wide** and the message panel flexible. Give the list and history independent scrolling. Use `min-width: 0`, `min-height: 0` and appropriate overflow rules to prevent long messages from breaking the layout.

## 4. Build conversation queries from imported sources

Use existing `Source` rows. No new conversation table or connector changes are expected.

Every query, including subqueries and contact lookups, must be scoped to the authenticated workspace.

Eligible messages must have:

```python
Source.provider == "whatsapp"
Source.deleted_at.is_(None)
Source.included.is_(True)
Source.conversation_id.is_not(None)
```

Identify a conversation by **both `provider_account` and `conversation_id` within the workspace**. Do not group by title or merge messages from different linked accounts.

Build one row per conversation using SQL grouping/window functions, selecting its latest eligible message. Sort conversations newest first. Use a deterministic ordering, such as `coalesce(occurred_at, created_at)` followed by source ID.

Paginate the conversation list at 50 rows per page. Query summaries in SQL rather than loading all messages into Python. Avoid one database query per conversation.

Search should match conversation titles and resolved contact names, case-insensitively. Bound the input length and escape SQL wildcard characters. Apply search before pagination. Search is for finding conversations; opening one displays its full imported history.

Resolve direct-chat names from workspace-scoped `PersonIdentifier`/`Person` records where possible, so corrected contact names appear. Otherwise use the stored conversation title. For groups, use the conversation title.

Do not present opaque WhatsApp IDs as names. Use a readable fallback such as “Unnamed conversation.” Never infer that an `@lid` identifier is a phone number.

Each list row should show:

- Initials avatar or a generic group icon.
- Conversation name.
- Latest message time.
- One-line truncated preview.
- `You:` for outgoing messages; sender name for incoming group messages.
- Selected-state styling.

For non-text messages, derive a readable label from `Source.meta["kind"]`: “Voice message,” “Photo,” “Video,” “Document,” or “Non-text message.” Preserve captions when present. Do not display invented unread counts, delivery receipts or online status.

## 5. Implement the conversation reader and empty states

Selecting a conversation must load only that workspace/account/chat combination. Invalid or inaccessible selections should return the existing generic 404 behavior.

Show the newest 100 imported messages, rendered in chronological order. Provide “Older messages” pagination and a way back to the latest messages. A cursor based on an existing source ID is suitable; resolve and validate it within the selected conversation, then compare the same timestamp/ID ordering used by the query.

The reader should contain:

- Conversation header with its name and group indicator when applicable.
- Incoming messages aligned left and outgoing messages aligned right.
- Sender labels for incoming group messages.
- Message timestamps and date separators.
- Preserved line breaks and wrapping for long text and URLs.
- Non-text placeholders and captions.
- A discreet link from each message to its existing `/sources/{id}` detail page.

Keep Jinja autoescaping enabled. Render imported message content as text, never trusted HTML.

On initial latest-history load, a small progressive enhancement may scroll the history pane to the newest message. Older-history navigation must not immediately jump back to the bottom.

Provide these states:

- No selection: “Select a conversation” / “Choose a chat to read its imported history.”
- No imported messages and no connection: link to Connections to link WhatsApp.
- Connection exists but history is empty: explain that imported conversations will appear here.
- No search matches: clear message and a clear-search link.
- Disconnected with stored history: keep that history readable.

Use a short note that only imported history is available. Connection management and QR pairing remain on Connections. Page rendering should read stored data without making a live connector request.

Do not add a composer, Send button, attachment downloading or message-sending endpoints.

## 6. Make mobile behavior explicit

Below the desktop breakpoint:

- Show the conversation list when no chat is selected.
- Show the reader when a chat is selected.
- Include a visible “Back to conversations” link that preserves search/list state.
- Keep the expanded app navigation usable.
- Ensure only the active conversation pane participates in keyboard navigation.
- Verify no horizontal page scrolling at 390px width.

Preserve existing light/dark CSS variables. Match the reference's pane structure, spacing and selected-row treatment while retaining Moresocial's theme.

## 7. Verify the implementation

Add `tests/test_whatsapp_pages.py` covering authentication, workspace isolation, separate account/chat grouping, search, sorting, pagination, deleted/excluded messages, media placeholders and escaped message content. Include the case where two users have the same conversation identifier.

Extend browser coverage to verify:

- Desktop sidebar and WhatsApp active state.
- Mobile menu, conversation selection and back navigation.
- Direct links, refresh and browser Back.
- Search and older-history navigation.
- Existing logout and administrator visibility.
- No horizontal overflow or JavaScript errors.
- No sending controls.

Adapt existing browser tests to open the mobile navigation before clicking hidden menu links.

Run the repository's verification entry point from the repository root:

```bash
cd moresocial
bash scripts/test.sh
```

For visual review, run from `moresocial/`:

```bash
bash scripts/dev.sh
```

Use synthetic accounts and WhatsApp data. Inspect screenshots at **1440×900** and **390×844**, in light and dark mode, showing Home, the unselected WhatsApp page, and an open conversation.

Update `docs/architecture.md` with the new page and its read-only behavior. Finish with a concise summary of changed files, verification results and any remaining limitations. Implementation and local verification are the deliverable; deployment is a separate step.
