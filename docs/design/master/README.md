# Reference for publishing the Master dashboard

- design_master_dashboard.dc.html  the approved interactive design with all three tabs and one shared filter state. The script block holds the filter model; the markup is the layout. It is a Design Component page (needs that runtime): port the markup and the script, do not serve it as is.
- sample_data.json                 synthetic linked data the design runs on (67 villages, households, people, events, 24 sites, signals, clusters). Every number matches the approved screenshots.
- acceptance_tests.js              145 checks (node acceptance_tests.js): every click handler, every filter against an independent calculation, own-dimension rule, cascade, empty state, chips.
- template_spec.master.example.json  what Vima emits for the master (entity bindings, tabs, slots, filters, privacy). No layout code.

The design filters in the browser because its data is synthetic. The live template must aggregate on the server (see the prompt).
