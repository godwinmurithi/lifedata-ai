# LifeData Vima Powered Dashboards

Four-slide showcase: title, problem, solution and result. Suggested length: 7–9 minutes plus a 5–7 minute demo.

## Slide 1: LifeData Vima Powered Dashboards

Title slide — 45 to 60 seconds
This presentation showcases our recent work on LifeData, Vima and Superset. We started with an embedded conversational copilot that was constrained by its own tools and the surrounding platform setup. The goal was to make Vima a capable analyst and builder: able to use Superset's capabilities, explore data, build views and support a practical stakeholder experience.

The work spans the AI integration, Superset services and the dashboard interface. Codex and Claude supported implementation, investigation and validation. The following slides explain the starting problem, the changes we made and the concrete result available to demonstrate. The screenshot is a captured Buikwe workspace, not a live feed.

Today I will show both the engineering behind the change and what a user can now do: filter an area, explore a facility and refine a published chart through Vima. The development focus was staging; this presentation does not claim that every item in the original roadmap is production-ready.

## Slide 2: Problem: Vima was a constrained add-on

Opening — about 2 minutes
The original problem was architectural as well as usability-related. The first specification deliberately created a proof of concept around an unchanged Superset: nginx injected a JavaScript chat panel, a separate Django application ran an Anthropic tool-use loop, and custom Python tools wrapped the REST API. Reads used the viewer's session; validated writes went back to the browser for execution. This was a functioning copilot, but its capabilities were determined by the small tool surface and policy layer we had built around Superset.

That explains the phrase “an embed.” Vima was an injected interface attached to Superset, not a native Superset extension with the platform's full capability surface. It already had real REST integration and could create saved charts and dashboards; calling it a placeholder would be inaccurate. The limitation was the integration model and what the agent could reach. The early implementation also intentionally deferred MCP, so it was looping through its own custom tools rather than calling Superset's native MCP service.

Your September 25 Claude requests reveal why that became a problem. You asked whether Superset already had MCP, directed a switch to native MCP, and then asked why all MCP capabilities were not exposed to the chatbot. You pasted examples where Vima refused facility maps, could not apply dashboard styling and could not use datasets outside its catalogue. Some refusals came from our wrapper's allowlists and missing tools, rather than an absence of capability in Superset. Distinguish those causes: a missing boundary source is a data problem; a missing style tool is an integration problem; a blocked chart type is a policy problem. They were being experienced as one platform limitation.

Superset itself still presented a technical dashboard-building workflow. Earlier requests asked to reproduce the ArcGIS Buikwe experience, fit all content into a cockpit view, make it responsive and selectable, fix erroring charts, and add cascading geographic filters and useful map tooltips. Its staging deployment also lacked dedicated worker/beat services and screenshot rendering infrastructure. These gaps limited the quality of the end-to-end experience even when the copilot produced a chart.

Your expanded September 25 plan set a much broader goal: understand the data and the user's intent, reason through several queries, build full dashboards and bespoke visuals, research before extending Superset, and reuse approved work as skills. You explicitly asked to focus on empowering the AI within Superset first; stakeholder headless views were to follow. That is the project this presentation describes.

The central problem is therefore the gap between a constrained conversational add-on and the capable analyst and builder you requested. Prompt quality, layout bugs and undo failures mattered, but were symptoms and engineering tasks within that larger transformation.

## Slide 3: Solution: empower Vima and extend the platform

What changed — about 3 minutes
1. Native capability access. We enabled the MCP service shipped with Superset 6.1, added the required dependency and configured authentication. Testing exposed a real identity propagation gap: validating the token did not by itself make Superset tools run as the requested user. We built a per-user bridge and checked different users. Vima reads and writes now have native MCP paths, tool discovery and passthrough; REST/browser paths remain where MCP lacks a field or action. Dashboard CSS and theme wrappers were added rather than treating a missing MCP tool as proof the operation was impossible. The Django agent loop remains; the major change is the backend capability surface, not removal of all orchestration.

2. Remove artificial capability ceilings. We expanded agent-facing chart types and access, enabled development execution paths, broadened styling and eventually removed the fixed semantics registry and visualization/colour/layout restrictions. This addressed the frustrating situation in which Superset could reach a dataset but Vima could not build on it. These were staging decisions. Per-user permission checking is not a claim of production-ready privacy; the sandbox has real-data connections and later requests also removed test RLS filters. Historical RLS tests demonstrate what was tested then, not universal current isolation.

3. Analyst and builder extensions. We added wrappers for six additional chart types, a sandboxed canvas for bespoke visuals, query-log mining, continuous-analyst anomaly narratives, schema-related chart repair, exports and an approved-skill mechanism. A working canvas can be generalized and reapplied through a reviewed skill. This is broader than renaming a chart or returning one number. The full vision remains larger than the implementation: a universal plugin generator, automatic skill selection/gallery, comprehensive research tools and every depth-test target are not all complete.

4. Superset engineering. We added dedicated worker/beat and Redis infrastructure and verified thumbnail/screenshot rendering. We fixed MCP, query, missing-column and service crash issues. Async chart loading was disabled when it proved broken; describe the restored working query path, not a finished realtime push pipeline. The frontend dnd-kit migration was completed in source and tested, but that fork has not replaced the running Superset image. The custom dashboard editor uses dnd-kit separately.

5. Stakeholder experience. After prioritizing capability inside Superset, we built custom Vima publications from Superset data responses. Common charts render directly rather than through a complete dashboard iframe. Compact layouts, grouped tabs, geographic layers and shared filters use reusable components and client-specific data mappings. This is the implemented form of the hybrid direction: analysts retain native Superset, and stakeholders can use a focused custom workspace. The implemented UI is served by LifeData/Django with React components, not the originally proposed Next.js delivery. Custom-only publication later became the default; it need not create a second native dashboard unless requested.

6. Practical workflow. Stable publication links, targeted single-chart styling, persisted layout edits, screenshots and document attachments, better chat context and Anthropic/OpenAI/DeepSeek adapters make the workflow usable. Browser checks and regression tests verify real actions rather than just generated definitions. Codex and Claude contributed to implementation, investigation and validation across these areas; the history does not support assigning every feature to one assistant.

Transition: We can now show the combined result. The strongest live evidence is the existing dashboard workflow; advanced analyst extensions should be described with their actual maturity.

## Slide 4: Result: a connected analyst-and-dashboard workflow

Result — about 2 minutes, then demo
The result has two parts. The capability layer now connects Vima to Superset's native tools and richer extensions. The presentation layer now offers a reusable, interactive workspace on top of Superset's data. Together they support a practical flow: discover the data, build the view, publish it, explore it and refine it in conversation.

This is the progression in the original requests: first empower the AI inside Superset, then support focused stakeholder dashboards while keeping native Superset available to analysts. Today's implementation uses LifeData-hosted custom views, with custom-only publishing as the default. Native Superset charts/dashboards remain supported when requested. Existing custom publications can be updated at their stable URLs.

We can show a concrete result with Buikwe. In the recorded screenshot there are 47 reference facility locations, one county and 11 subcounties, alongside 80 assessment records. Records and distinct facility locations are different units, and live totals may change. A subcounty selection filters the same cohort across the map, charts and matching records. Map layers can be toggled; related service questions fit into tabs; Not recorded remains distinct from No. A facility popup connects geographic context to its recorded attributes.

The same components have supported Buikwe facility/population views, Trans Nzoia facility intelligence, SINCEP assessment and UCMB geographic filtering. Shared presentation does not mean shared data: each publication maps its client's real source fields. PCN/hub filters and map layers are optional and depend on actual source values.

The broader analyst work includes anomaly narratives, query-log suggestions, chart repair, export packages, custom canvas and approved skills. Explain these as implemented supporting capabilities with different maturity levels. They are not evidence that Vima can autonomously solve every deep analysis task in the original plan. We have not established a current model-success benchmark or measured build-time savings.

One architectural distinction matters: Vima still has orchestration and a chat interface. We expanded what those layers can do and added custom dashboard surfaces. We did not replace Superset's entire frontend or turn Vima into a native Superset extension. The Superset source migration is not deployed as the current image. Guest-token scoping was tested historically; test RLS was later removed by request, so do not present it as an active universal guarantee.

Demo three observable outcomes: choose a subcounty and show coordinated filtering; inspect a facility and toggle a boundary layer; ask Vima to change only the assessment-records gauge to green, then reload to show the saved change. These prove an interactive view and conversational refinement. They do not prove anomaly detection or reusable-skill execution; use the notes and supporting report for those questions.

Remaining limits: unsupported custom chart types have a labelled table fallback, loaded workspace records are capped at 10,000 with a label, uploaded spreadsheets are not automatic imports, and production privacy/permissions need their own readiness assessment. The sandbox public health check showed liveness with DeepSeek configured, not proof that every provider account or every model request succeeds.

Closing: We have moved the system beyond the original constrained add-on: Vima has broader access to platform capabilities, and users have an interactive published dashboard they can explore and refine. Let us demonstrate what works today.

# Demo walkthrough — 5 to 7 minutes

Start at https://sandbox.lifehealth.app/lifedata-ai/dashboards/ and sign in through Superset. Open the Buikwe facility workspace. The publication used by the recorded browser check is https://sandbox.lifehealth.app/lifedata-ai/dashboards/3e4fa498-c8e5-4fe5-aea7-80a34f24f292/ . If that link is unavailable, choose the current Buikwe publication from the library.

| Time | Action | What to say / evidence to show |
| --- | --- | --- |
| 0:00–0:45 | Open the existing dashboard; reset filters. | “This is a Vima workspace using Superset data. The cards, map and details belong to one view.” Distinguish assessment records from facility locations. |
| 0:45–1:45 | Choose a subcounty that has records. | “One selection narrows the same cohort across the dashboard.” Show the match count, map markers and service charts changing. |
| 1:45–2:45 | Hover or click a facility; open the map layers control. Toggle a boundary layer. | “The marker shows facility context; boundaries give geographic context.” Show recorded level/bed information where present. Use Select facility if available. |
| 2:45–3:30 | Reset; switch between tabs in a service card. | “Related questions fit in one card. Missing data stays visible.” Point out counts, percentages and Not recorded. |
| 3:30–5:00 | Open Ask Vima. Send: “On this dashboard, change only the assessment-records gauge to green. Keep all other charts and filters as they are.” | “The same conversation can refine the published view.” Complete the displayed action flow, then confirm only the gauge changed. Allow up to 90 seconds; do not promise an instant response. |
| 5:00–5:30 | Reload the page. | “The change is saved to the publication.” Confirm the colour persists. |
| Optional | Ask: “Describe the data sources and filters used by this dashboard.” | Show that Vima can inspect and explain the dashboard, if model service access is working. |

Before the audience arrives: open and sign in to the actual publication; confirm the map and filters load; reset any old selections; check that Ask Vima responds; have the PDF and demo-dashboard.png open locally. Use an account that owns the publication for the style change. Keep the dashboard in compact layout. Test the prompt once and restore the gauge with “Reset only the assessment-records gauge styling to its default.” Do not switch providers or rebuild the stack immediately before the talk.

If the model stalls: continue the filter/map/tab demo and say “The saved dashboard is interactive independently of the chat response.” If network or login fails: open demo-dashboard.png and explain the same three interactions. The screenshot is a recorded fallback, not an interactive demo. Do not use synthetic counts as live results.

Optional build prompt, after the main demo: “Using the dataset behind this dashboard, create a separate demo publication in the custom Vima UI with the standard compact layout. Include facility level and ownership, relevant service questions, and shared area filters. Use only recorded geography, and show missing answers explicitly.” This can take several minutes and creates artifacts, so keep it outside the core timed demonstration.


# Supporting record of recent work

Prepared 7 October 2026 from local repositories and implementation reports. The recorded development window reviewed here is 25 September–6 October; the original copilot predates the first repository commit. This is a product summary, not a claim that every roadmap phase is production-ready.

| Area | What was implemented | Evidence / scope |
| --- | --- | --- |
| Superset integration | Native MCP service, per-user JWT bridge, read/write adapters, tool discovery and passthrough; browser/REST paths where needed | AI_NATIVE_STATUS.md, Sep 25 updates; superset-staging commits 41f24cb and following |
| Conversational workflows | Dataset discovery, chart/dashboard actions, styling, context, audit/provenance, canvas and reusable skills | lifedata_ai tools/agent and Sep 25–30 implementation reports |
| Operations | Worker/beat, dedicated Redis, thumbnails; async crash-loop fixes | superset-staging a6f6ce9 and bf7ad5c; async chart loading disabled |
| Analyst extensions | Query-log mining, continuous analyst scheduler, simulated stream, repair command, guest-token scoping and export packages | Sep 26–Oct 1 reports and commits; maturity varies, no production CDC claim |
| Frontend migration | Superset source migrated to dnd-kit; build and focused regression validation | Sep 30 report and Oct 1 follow-up; source fork is not the deployed Superset image |
| Custom publications | Live custom chart rendering, stable links, themes, persisted layout editing, attachment handling | custom-dashboards-and-uploads.md; Oct 1/6 implementation |
| Shared design and maps | Versioned design system, tabbed cards, targeted chart styling, facility markers and boundaries | vima-design-system.md; 7f01f33 |
| Publishing defaults | Custom-only publishing using minimal reusable data sources | d77333b |
| Geographic hierarchy | Boundary discovery, spatial assignment, cascading filters, optional PCN/hub | 2564965 and 433ec13 |
| Layout | Original compact benchmark retained; resizing opt-in | 9782b32 |
| Model adapters | Anthropic, OpenAI and DeepSeek support | docs/model-providers.md; 3902b5b and provider source files |

Original-request context reviewed for this revision:
- Early POC specification: injected panel, Django/Anthropic custom tool loop, REST tools, MCP deliberately deferred.
- Claude session 308e2a22, 25 September: initial AI-native plan; switch to native MCP; examples of blocked maps and missing style tools.
- Claude session 1a8661bf, 25 September 19:44 UTC: expanded analyst/builder north star and 18-phase plan. Later instruction prioritized AI empowerment before headless work.
- Claude session 6598502e, 1 October: registry-related dataset refusal, chat UX/attachments and service crash fixes.
- Claude session f1f13301, 2 October: improve the custom dashboard home and present the full AI-native Superset progress.
- Later September 30 session instructions removed test RLS; historical scoping verification must not be represented as universal current isolation.

Architecture before: browser / injected panel → Django custom agent tools → Superset REST → native dashboard UI.
Architecture now: browser / Vima and custom workspace → LifeData orchestration → native MCP plus required REST/browser paths → Superset query/access engine → native or custom dashboard surface.

The original plan proposed a Next.js hybrid frontend. The delivered custom dashboard surface is the LifeData/Django-hosted React UI. The original plan also requested capabilities beyond the implemented scope; the notes distinguish those goals from verified work.

Primary source files:
- /home/geonode/projects/lifedata-ai/AI_NATIVE_STATUS.md (historical updates; read later sections before relying on earlier findings)
- /home/geonode/projects/lifedata-ai/docs/custom-dashboards-and-uploads.md
- /home/geonode/projects/lifedata-ai/docs/vima-design-system.md
- /home/geonode/projects/lifedata-ai/docs/model-providers.md
- /home/geonode/projects/superset/docs/dashboard-playbook.md (native Superset layout/filter work)
- /tmp/check_vima_buikwe_workspace_map.py and /tmp/vima-buikwe-workspace-map.png (recorded live browser check and screenshot)

The presentation relies on repository history, code and recorded validation. The historical browser check was inspected, not rerun for this presentation. No claims are made about quantified time savings, all-provider account readiness, full production privacy compliance or complete parity with Superset's plugin catalogue.
