from app.routers.compute import models as cm

from activate_iri.schedulers import JobState, PBSScheduler, SlurmScheduler


def spec(**kw):
    base = {"executable": "/usr/bin/python", "arguments": ["train.py", "--epochs", "3"], "name": "train", "directory": "/home/u/work",
            "environment": {"OMP_NUM_THREADS": "4"}, "stdout_path": "/home/u/work/out.log",
            "resources": cm.ResourceSpec(node_count=2, processes_per_node=4, cpu_cores_per_process=8, gpu_cores_per_process=1, memory=64 * 1024 ** 3),
            "attributes": cm.JobAttributes(duration=7200, queue_name="gpu", account="amsc-materials", custom_attributes={"constraint": "h100"}),
            "launcher": "srun", "pre_launch": "module load cuda"}
    base.update(kw)
    return cm.JobSpec(**base)


def test_slurm_script_from_psij_spec():
    s = SlurmScheduler().script(spec())
    for directive in ["#SBATCH --nodes=2", "#SBATCH --ntasks-per-node=4", "#SBATCH --cpus-per-task=8", "#SBATCH --gpus-per-task=1",
                      "#SBATCH --mem=65536M", "#SBATCH --time=02:00:00", "#SBATCH --partition=gpu", "#SBATCH --account=amsc-materials",
                      "#SBATCH --constraint=h100", "#SBATCH --chdir=/home/u/work", "module load cuda", "export OMP_NUM_THREADS=4",
                      "srun /usr/bin/python train.py --epochs 3"]:
        assert directive in s, directive


def test_slurm_container_job_uses_apptainer_with_gpu():
    s = SlurmScheduler().script(spec(container=cm.Container(image="docker.io/library/python:3.12", volume_mounts=[cm.VolumeMount(source="/data", target="/mnt/data")])))
    assert "apptainer exec --nv --bind /data:/mnt/data:ro docker://docker.io/library/python:3.12 /usr/bin/python train.py" in s


def test_slurm_parsing():
    sch = SlurmScheduler()
    assert sch.parse_submit("12345;cluster\n") == "12345"
    live = "12345|RUNNING|train|gpu|2|None\n"
    rec = sch.parse_status("12345", live)
    assert rec.state == JobState.ACTIVE and rec.nodes == 2 and rec.partition == "gpu"
    hist = "12345|COMPLETED|train|gpu|2|0:0\n12345.batch|COMPLETED|batch|gpu|2|0:0\n"
    rec = sch.parse_status("12345", hist)
    assert rec.state == JobState.COMPLETED and rec.exit_code == 0
    assert sch.parse_status("7", "7|CANCELLED by 1001|x|cpu|1|0:0").state == JobState.CANCELED
    assert sch.parse_status("8", "8|OUT_OF_MEMORY|x|cpu|1|0:125").state == JobState.FAILED


def test_pbs_script_and_parsing():
    s = PBSScheduler().script(spec())
    assert "#PBS -l select=2:mpiprocs=4:ncpus=32:ngpus=4:mem=65536mb" in s
    assert "#PBS -l walltime=02:00:00" in s and "#PBS -q gpu" in s and "#PBS -A amsc-materials" in s
    out = '{"Jobs": {"42.polaris": {"job_state": "R", "Job_Name": "train", "queue": "gpu"}}}'
    rec = PBSScheduler().parse_status("42.polaris", out)
    assert rec.state == JobState.ACTIVE and rec.partition == "gpu"
    done = '{"Jobs": {"42.polaris": {"job_state": "F", "Exit_status": 1}}}'
    assert PBSScheduler().parse_status("42.polaris", done).state == JobState.FAILED


def test_slurm_status_falls_back_to_scontrol_and_rc_file():
    sch = SlurmScheduler()
    out = "SCONTROL|JobId=77 JobName=train JobState=COMPLETED Partition=debug NumNodes=1 ExitCode=0:0 Reason=None\n"
    rec = sch.parse_status("77", out)
    assert rec.state == JobState.COMPLETED and rec.exit_code == 0 and rec.partition == "debug"
    assert sch.parse_status("78", "78|RCFILE|3\n").state == JobState.FAILED
    assert "scontrol -o show job 77" in sch.status_command("77", True) and "RCFILE" in sch.status_command("77", True)
    assert 'echo $__iri_rc > "$SLURM_SUBMIT_DIR/rc"' in sch.script(spec())


def test_pbs_status_rc_file_fallback():
    sch = PBSScheduler()
    assert sch.parse_status("9.polaris", "\n9.polaris|RCFILE|0\n").state == JobState.COMPLETED
    assert '"$PBS_O_WORKDIR/rc"' in sch.script(spec())


def test_apptainer_nv_without_gres_directive():
    s = SlurmScheduler().script(spec(resources=cm.ResourceSpec(node_count=1), container=cm.Container(image="nvidia/cuda:12.4.1-base-ubuntu22.04"),
                                     attributes=cm.JobAttributes(queue_name="debug", custom_attributes={"apptainer-nv": "true"})))
    assert "apptainer exec --nv" in s and "--gpus-per-task" not in s and "--apptainer-nv" not in s
