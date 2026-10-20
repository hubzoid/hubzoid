"""repeated_request: "repeat this for me", a person's own Markdown-style workflow.

Saved from chat ("every Monday at 9, summarise low stock") with the usual
two-message confirmation, then run on its schedule as its owner, with their
access checked again before every run. Each run asks the hub's agent the
saved request, fresh (no chat history), and puts the answer in a new private
chat for its owner. Unattended: a step that needs a person's approval is
refused and the answer says so.

Who may save one is workflow access: ``run`` and ``schedule`` on
``repeated_request`` (Settings, Workflow access).
"""
from pydantic import BaseModel, Field

from hubzoid import hub, step, workflow


class Request(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=16000,
                        description="Exactly what to do each time, as you would ask it in chat.")


@step()
def _deliver(prompt: str, answer: str):
    """One durable step, so a restarted run doesn't post the answer twice."""
    from hubzoid.workflows.builtin import deliver

    return deliver.to_private_chat(prompt, answer)


@workflow(as_tool=True, schedulable=True, inputs=Request, surfaces=("web",), min_interval="1h")
def repeated_request():
    """Repeat a request for you on a schedule."""
    prompt = hub.input.prompt
    answer = hub.call_agent(prompt)
    chat = _deliver(prompt, answer)
    return {"answer": answer, "chat": chat}
