# Chameleon implementation plan

The [local pilot](PILOT.md) captured 840 prediction requests from two GourmetGram servers on a Mac. The next step is to run the same collector and validator inside the Chameleon Kubernetes cluster. In the existing pipeline, the new evaluation step fits after the candidate reaches Canary and before Production promotion. [Original promotion workflow](https://teaching-on-testbeds.github.io/mlops-chi/#promotion-to-production)

| Order | Change | Evidence that it works |
|---|---|---|
| 1. Run on Chameleon | Package `collect.py` and `gate.py` in a small Python container. Provide the Food-11 images and manifest as inputs, and point `targets.json` at two isolated GourmetGram services on the cluster. Confirm the model versions and resolved image IDs before testing. | A paired run records actual HTTP requests, request pacing, deployment versions, and a decision that can be replayed from its files. |
| 2. Automate evaluation | Add an Argo Workflows step that runs the collector, then the gate. Run in evaluation-only mode while collecting the case-study data. Start with an identical-model control and a delayed candidate. | Argo records each run and its decision. Both comparisons use the same workload for baseline and candidate. |
| 3. Save evidence in MLflow | Create an evaluation run for each pair. Log model versions, workload settings, metrics, and the decision. Attach `requests.csv`, `trial.json`, `decision.json`, and the workload manifest. | A reviewer can trace a result to its input data and replay the decision. |
| 4. Connect to promotion | After validating evaluation-only runs, test promotion in an isolated environment. Pass the saved decision to Argo and allow the existing `promote-model` step only on a valid PASS for the current candidate version. Argo CD applies the existing deployment change. | PASS allows promotion when enabled. FAIL, HOLD, a timeout, or a missing decision blocks promotion. |

The workflow will save and surface FAIL and HOLD as evaluation outcomes. It will stop promotion if collection or validation crashes. Argo can use a saved output value to decide whether to run the promotion step. [Argo output parameters](https://argo-workflows.readthedocs.io/en/latest/walk-through/output-parameters/) · [Argo conditionals](https://argo-workflows.readthedocs.io/en/latest/walk-through/conditionals/)

`build_workload.py` prepares separate Food-11 image lists for pilot and final trials. `pilot/serve.py` starts the Mac demo servers. On Chameleon, Kubernetes runs the GourmetGram deployments. The [case study protocol](CASE_STUDY.md) defines the repeated trials and how to analyze them.
