# activate-iri-connector

The consumer side of the integration: ACTIVATE reaching facilities that already run a DOE IRI
Facility API endpoint (ALCF, NERSC, OLCF, ESnet, other PW facility endpoints).

* `workflow/iri-job/workflow.yaml`: an ACTIVATE workflow that submits a PSI/J job to any IRI
  facility, polls it, and returns stdout through the IRI filesystem task loop. Users pick the
  facility and resource in the form; the facility token is an account variable. Because it is
  an ordinary workflow, it composes with every other ACTIVATE workflow: a DAG can stage data on
  a PW cloud cluster, run the large job on Polaris, and post-process on a NeoCloud GPU node.
* `tools/iri_facilities.py`: a cross-facility standboard that lists resources, status, and open
  incidents across all reachable IRI endpoints, ACTIVATE's own included. The same calls back the
  ACTIVATE UI panel described in the integration plan.

A later increment adds an ACTIVATE resource type "IRI facility" so that facility resources appear
in the Compute list with live status and allocations; the workflow above needs no platform change.
