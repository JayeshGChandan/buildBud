from dotenv import load_dotenv
from langchain_core.globals import set_verbose, set_debug
from langchain_groq.chat_models import ChatGroq
from langgraph.constants import END
from langgraph.graph import StateGraph

from agent.prompts import *
from agent.states import *
from agent.tools import (
    write_file, read_file,
    get_project_context, save_project_state, load_project_state,
)

_ = load_dotenv()

set_debug(True)
set_verbose(True)

llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0)


def strip_code_fences(text: str) -> str:
    """Removes a surrounding markdown code fence if the model added one.

    The coder is told to emit raw file contents, but models sometimes wrap the
    output in ```lang ... ```. We strip only a fence that wraps the whole
    response so real backticks inside the file are left untouched.
    """
    t = text.strip()
    if not t.startswith("```"):
        return text.strip()
    lines = t.split("\n")
    lines = lines[1:]  # drop opening ``` (and any language tag)
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]  # drop closing ```
    return "\n".join(lines).strip()


def planner_agent(state: dict) -> dict:
    """Converts user prompt into a structured Plan."""
    user_prompt = state["user_prompt"]
    resp = llm.with_structured_output(Plan).invoke(
        planner_prompt(user_prompt)
    )
    if resp is None:
        raise ValueError("Planner did not return a valid response.")
    return {**state, "plan": resp}


def architect_agent(state: dict) -> dict:
    """Creates TaskPlan from Plan."""
    plan: Plan = state["plan"]
    resp = llm.with_structured_output(TaskPlan).invoke(
        architect_prompt(plan=plan.model_dump_json())
    )
    if resp is None:
        raise ValueError("Planner did not return a valid response.")

    resp.plan = plan
    print(resp.model_dump_json())
    # Persist so later update runs can stay consistent with this build.
    save_project_state(plan.model_dump(), resp.model_dump())
    return {**state, "task_plan": resp}


def analyzer_agent(state: dict) -> dict:
    """Reads the existing project into a text snapshot for the update planner."""
    return {**state, "project_context": get_project_context()}


def modify_planner_agent(state: dict) -> dict:
    """Produces a scoped TaskPlan for a change to the existing project."""
    user_prompt = state["user_prompt"]
    project_context = state["project_context"]
    resp = llm.with_structured_output(TaskPlan).invoke(
        modify_planner_prompt(user_prompt, project_context)
    )
    if resp is None:
        raise ValueError("Update planner did not return a valid response.")
    # Carry forward the saved plan summary (tech stack / features) if present.
    saved = load_project_state()
    plan_dump = saved.get("plan") if saved else None
    print(resp.model_dump_json())
    save_project_state(plan_dump, resp.model_dump())
    return {**state, "task_plan": resp}


def coder_agent(state: dict) -> dict:
    """Generates one file's full contents as plain text and writes it to disk."""
    coder_state: CoderState = state.get("coder_state")
    if coder_state is None:
        coder_state = CoderState(task_plan=state["task_plan"], current_step_idx=0)

    steps = coder_state.task_plan.implementation_steps
    if coder_state.current_step_idx >= len(steps):
        return {**state, "coder_state": coder_state, "status": "DONE"}

    current_task = steps[coder_state.current_step_idx]
    existing_content = read_file.run(current_task.filepath)

    system_prompt = coder_system_prompt()
    preserve_note = (
        "This is an UPDATE to an existing app. Preserve ALL existing "
        "functionality and only apply the requested change. Write back the "
        "COMPLETE file including the unchanged parts.\n"
        if state.get("mode") == "update" else ""
    )
    existing_block = (
        f"Existing content of this file:\n{existing_content}\n"
        if existing_content else "This file does not exist yet; create it.\n"
    )
    user_prompt = (
        f"Task: {current_task.task_description}\n"
        f"File to write: {current_task.filepath}\n"
        f"{existing_block}"
        f"{preserve_note}"
        f"Now output the complete contents of {current_task.filepath}."
    )

    # Generate the file body as PLAIN TEXT (no tool/function calling). This
    # avoids Groq's tool-call JSON serialization, which intermittently fails
    # with a 400 (tool_use_failed) on large, heavily-escaped file contents.
    # We write the file ourselves instead of relying on the model to call a tool.
    try:
        resp = llm.with_retry(stop_after_attempt=3).invoke(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}]
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        content = strip_code_fences(content)
        write_file.run({"path": current_task.filepath, "content": content})
    except Exception as e:
        # Degrade gracefully: if a single file keeps failing after all retries,
        # skip it and move on instead of aborting the whole build/update.
        failed = list(coder_state.failed_steps or [])
        failed.append(current_task.filepath)
        coder_state.failed_steps = failed
        print(f"WARNING: coder step failed for {current_task.filepath}: {e}")

    coder_state.current_step_idx += 1
    return {**state, "coder_state": coder_state}


graph = StateGraph(dict)

graph.add_node("planner", planner_agent)
graph.add_node("architect", architect_agent)
graph.add_node("analyzer", analyzer_agent)
graph.add_node("modify_planner", modify_planner_agent)
graph.add_node("coder", coder_agent)

# Build path: planner -> architect -> coder
graph.add_edge("planner", "architect")
graph.add_edge("architect", "coder")

# Update path: analyzer -> modify_planner -> coder
graph.add_edge("analyzer", "modify_planner")
graph.add_edge("modify_planner", "coder")

graph.add_conditional_edges(
    "coder",
    lambda s: "END" if s.get("status") == "DONE" else "coder",
    {"END": END, "coder": "coder"}
)

# Route to the build or update pipeline based on the detected mode.
graph.set_conditional_entry_point(
    lambda s: s.get("mode", "build"),
    {"build": "planner", "update": "analyzer"},
)
agent = graph.compile()
if __name__ == "__main__":
    result = agent.invoke({"user_prompt": "Build a colourful modern todo app in html css and js"},
                          {"recursion_limit": 100})
    print("Final State:", result)
