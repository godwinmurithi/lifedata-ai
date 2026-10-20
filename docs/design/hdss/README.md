# Reference for publishing the HDSS template

- design_hdss_template.dc.html  the approved interactive design. Layout = markup; the filter model and deep-dive links = the script block.
  It is a Design Component page (needs that runtime), so port the markup and the script, do not serve it as is. Images are in assets/.
- sample_data.json              synthetic linked data the design runs on (villages, households, people). Every number matches the approved screenshot.
- acceptance_tests.js           160 checks (node acceptance_tests.js): every click handler, every filter against an independent calculation, deep-dive links.
- template_spec.hdss.example.json  what Vima emits for an HDSS dashboard (bindings, slots, filters, deep-dive addresses, privacy). No layout code.
- assets/                       the LifeConnect logo and the deep-dive icon supplied by the user.

The design filters in the browser because its data is synthetic. The live template must aggregate on the server (see the prompt).
