# Edge mode: an IRI endpoint for a facility with nothing deployed

What the operator does on one login node:

    export PW_NODE_TOKEN=... PW_SERVICE_API_KEY=pwt_... IRI_NAME=exlab-iri IRI_CLUSTER=labcluster
    sudo -E ./install-edge.sh

What happens:

1. `pw agent register --systemd` connects the node to ACTIVATE over outbound HTTPS/WebSocket on
   port 443. From then on ACTIVATE sees partitions, jobs, node metrics, and mounted filesystems,
   and can push user, group, SSH-key, and sudo state to the node (the platform's access
   management, which is what makes "run as the calling user" possible without facility work).
2. The `activate-iri` container starts on the node with host networking and the Slurm client
   binaries mounted in. Edge mode publishes only this cluster.
3. `pw endpoints http --public --keep` registers a reverse tunnel and the endpoint answers at
   `https://<IRI_NAME>.activate.pw/api/v2`. No inbound port, no DNS or TLS work at the facility.

Identity: AmSC Keycards are validated by the reference framework (`AMSC_TOKEN_*`) and mapped to a
local account by `amsc_project_mapping.json`; ACTIVATE API keys are accepted as the
facility-specific credential. Filesystem and scheduler commands run through `sudo -n -u <user>`
under the sudoers rule installed by the script; replace `sudo` with an SSH CA (federation mode)
if the site policy prefers certificates.

Remove everything: `systemctl disable --now activate-iri-endpoint activate-iri pw-agent`, delete
`/etc/activate-iri` and `/etc/sudoers.d/activate-iri`, and delete the managed cluster in ACTIVATE.
