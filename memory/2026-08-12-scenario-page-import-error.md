# Debug report: scenario page import error

- **Symptom:** Streamlit failed at `visualization/app.py:41` while importing `show_scenario_page`.
- **Root cause:** `visualization.scenario_page` does define the callable at module scope. The failing `stock` interpreter could not finish importing that module because the strict V3 portfolio optimizer imports `scipy.optimize.milp`, while `scipy` (and the next required package, `scikit-learn`) were absent from that environment. A dependency-complete Anaconda interpreter imported the same source successfully.
- **Fix:** Synchronized `C:\Users\29643\Anaconda3\envs\stock\python.exe` with the exact versions in the repository `requirements.txt`. Added a runtime-import regression assertion in `tests/test_scenario_engine.py`.
- **Evidence:** In the repaired interpreter, direct import resolves `visualization.scenario_page.show_scenario_page`; SciPy is `1.15.3`, scikit-learn is `1.6.1`; Streamlit `AppTest.from_file("visualization/app.py")` reports zero exceptions.
- **Regression test:** `ScenarioEngineIntegrationTests.test_frontend_module_exports_callable_scenario_page` in `tests/test_scenario_engine.py`.
- **Test result:** `python -m pytest tests -q` -> 138 passed and 20 subtests passed. An unscoped repository-wide collection also enters the separately vendored `MiroFish` application and requires its own Flask environment; that collection boundary is unrelated to this import failure.
- **Environment note:** The shared `stock` environment contains extra packages outside the core lock whose declared requirements conflict with the core pins. The application and its full primary suite pass, but a clean application-only environment remains preferable to sharing one interpreter with those tools.
- **Status:** DONE_WITH_CONCERNS
