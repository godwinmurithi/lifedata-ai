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
