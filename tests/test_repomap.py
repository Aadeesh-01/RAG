import pytest

from coderag import RAGCodebaseAgent, get_config
from coderag.repomap import is_broad_question, repo_map
from conftest import FakeIndex, FakeLLM


@pytest.mark.parametrize("question", [
    "What does this project do?", "What are the main modules?", "Give me an overview",
    "How is the codebase organized?", "Explain the architecture", "What's in this repo?",
    "What is the structure of this repo?",
])
def test_broad_questions_are_detected(question):
    assert is_broad_question(question)


@pytest.mark.parametrize("question", [
    "Who calls resolve_redirects?", "What does normalize_name do?",
    "How does the structure of Session work?", "Where is the main loop?",
])
def test_specific_questions_are_not(question):
    assert not is_broad_question(question)


def doc(qname, kind, called_by=()):
    rel, name = qname.split("::")
    return {"qname": qname, "type": kind, "name": name.split(".")[-1], "called_by": list(called_by)}


def test_map_lists_every_area_with_sizes_and_entry_points():
    docs = [doc("PathPlanning/AStar/a_star.py::plan", "function"),
            doc("PathPlanning/RRT/rrt.py::RRT", "class"),
            doc("PathPlanning/RRT/rrt.py::RRT.planning", "method"),
            doc("SLAM/EKFSLAM/ekf.py::main", "function"),
            doc("utils/angle.py::rot_mat_2d", "function", called_by=["plan"]),
            doc("setup.py::main", "function", called_by=["x"])]          # called: not an entry
    text = repo_map(docs)
    assert "5 files, 5 functions and methods, 1 classes." in text
    assert "PathPlanning/  2 files, 2 functions: AStar/, RRT/" in text
    assert "SLAM/  1 file, 1 function: EKFSLAM/" in text
    assert "utils/  1 file, 1 function: angle" in text
    assert "(top level)  1 file, 1 function: setup" in text
    assert "Entry points: main (SLAM/EKFSLAM/ekf.py)" in text


def test_many_script_mains_are_summarised():
    docs = [doc(f"scripts/s{i}/run{i}.py::main", "function") for i in range(12)]
    assert "Entry points: 12 scripts each have their own main()." in repo_map(docs)


def test_broad_questions_get_the_map_in_context(sample_repo):
    agent = RAGCodebaseAgent(sample_repo, get_config("v8"), index=FakeIndex(),
                             llm=FakeLLM(), verbose=False)
    # the web app always asks for numbered, cited sources
    broad = next(agent.stream("What are the main modules?", llm=None, prompt_style="cited"))
    specific = next(agent.stream("What does normalize_name do?", llm=None, prompt_style="cited"))
    legacy = next(agent.stream("What are the main modules?", llm=None))
    assert broad["repo_map"] and broad["context"].startswith("REPO MAP")
    assert not specific["repo_map"] and "REPO MAP" not in specific["context"]
    assert not legacy["repo_map"]                    # small local models keep their tight context
