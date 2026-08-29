# amscrot-activate

An AmSCROT `ServiceClient` for Parallel Works ACTIVATE. AmSCROT is the AmSC Infrastructure
Services Resource Orchestration Toolkit (`doe-iri/iri-facility-api-toolkit`, `pip install
amscrot-py`), the client library the AmSC orchestrator and its Airflow operators use to discover
facilities and run jobs.

Two ways AmSC orchestration reaches ACTIVATE:

1. Through the IRI contract (no code here): an `AMSC_IRI` credential profile pointing at
   `https://iri.activate.pw/api/v2`. `IriServiceClient` discovers PW resources, submits PSI/J
   jobs, and fetches output like it does for NERSC or ESnet. This is the default and the one
   that fits the IRO's Lambda Worker model.
2. Natively (this package): `ActivateServiceClient` uses the ACTIVATE REST API and `pw` CLI for
   what the IRI contract does not carry yet: provider and region choice, elastic cluster sizing,
   and ACTIVATE allocation balances. It maps to the same `discover / plan / create / status /
   destroy` lifecycle, so a Session can mix an ACTIVATE job with IRI jobs at lab facilities.

Registration until upstreamed:

    from amscrot.util.constants import Constants
    Constants.SERVICE_CLIENT_CLASSES["activate"] = "amscrot_activate.activate_service_client.ActivateServiceClient"

`~/.amscrot/credentials.yml`:

    pw-activate:
      client_type: ACTIVATE
      api_key: pwt_...
      api_endpoint: https://activate.parallel.works
      workflow: iri-job-runner
