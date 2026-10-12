# Skills

A skill is a named piece of instructions — how to write SQL against this
warehouse, how to lay out a dashboard, how to brief a banker on a customer —
that agents use only when a task calls for it. Skills keep system prompts short
and let one well-written instruction serve every agent that needs it.

kaboo-workflows has no `skills:` field of its own. Skills are composed into the
workflow on the host, before the run is submitted — an application of
[Compose a workflow per run](per-run-workflows.md). This recipe is the pattern
Kaboo runs: a skill catalogue in a database, three ways to load a skill, and a
single tool agents call to load one.

## Three loading strategies

| Strategy | What happens | Use it for |
|----------|--------------|------------|
| `full` | Inlined into the agent's system prompt as top-priority rules | Short rules that must always apply: investigation discipline, house style |
| `catalog` | Listed by name and when-to-use; the agent loads it with a tool call | Guides only some tasks need: a tool manual, a web research protocol |
| `rag` | Listed like a catalog skill; the agent loads matching sections by query | Long references: an SDK manual, a widget reference |

Every agent's prompt ends with the skills it has *not* loaded, so an agent can
always discover the one that fits — and most skills cost nothing until used.

## Which skills an agent gets

- **Bound** in the stored workflow, with a host-side key the composer removes
  before submitting:

    ```yaml
    agents:
      sql_analyst:
        skills: [sql-guide, efficiency-discipline]
        mcp: [sql, skills]
    ```

- **Activated by tags.** A skill tagged with a data source's name or kind is
  loaded whenever the run has that data source attached — a skill tagged
  `offers-redshift` switches on for every run that attaches the connection
  named `offers-redshift`. We scope tag-activated skills to the agents that
  work with that kind of data, and the orchestrator always qualifies.
- **Gated by tools.** A skill that only makes sense with certain MCP clients
  (`requires_clients`) is only offered to agents that hold one of them.

## The composer

```python
from dataclasses import dataclass, field

CATALOG_HEADER = (
    "\n\n<available_skills>\n"
    "These skills are NOT loaded yet. Before starting any task, check whether one\n"
    "of them covers it, and call `load_skill` with its name if so. Do the work only\n"
    "after the relevant skill is loaded. If a skill you loaded earlier is no longer\n"
    "visible in your context (long runs compress old messages), load it again.\n\n"
)


@dataclass
class Skill:
    name: str
    description: str
    content: str
    strategy: str = "full"
    rules: str | None = None
    tags: list[str] = field(default_factory=list)
    requires_clients: list[str] = field(default_factory=list)


def compose_skills(doc: dict, skills: dict[str, Skill], active_tags: set[str]) -> None:
    for agent in doc.get("agents", {}).values():
        bound = agent.pop("skills", [])
        held = set(agent.get("mcp", []))

        def usable(skill: Skill) -> bool:
            return not skill.requires_clients or bool(held & set(skill.requires_clients))

        names = [name for name in dict.fromkeys(bound) if name in skills]
        names += [
            s.name
            for s in skills.values()
            if s.name not in names and set(s.tags) & active_tags and usable(s)
        ]

        full = [skills[n] for n in names if skills[n].strategy == "full"]
        if full:
            rules = "\n\n".join(f"### {s.name}\n{s.content}" for s in full)
            agent["system_prompt"] = (
                f'<rules priority="1">\n{rules}\n</rules>\n\n'
                f"<agent_instructions>\n{agent['system_prompt']}\n</agent_instructions>"
            )

        catalog = [
            s
            for s in skills.values()
            if (s.name in names and s.strategy != "full") or (s.name not in names and usable(s))
        ]
        if catalog:
            lines = "\n".join(f"- **{s.name}**: {s.rules or s.description}" for s in catalog)
            agent["system_prompt"] += CATALOG_HEADER + lines + "\n</available_skills>"


SKILLS = {
    "efficiency-discipline": Skill(
        "efficiency-discipline", "Avoid redundant work", "Never repeat a tool call."
    ),
    "sql-guide": Skill(
        "sql-guide", "SQL patterns", "...", strategy="rag", rules="Use when writing SQL"
    ),
    "nbo-analysis": Skill(
        "nbo-analysis", "Offer revenue analysis", "Quantify every claim.", tags=["offers"]
    ),
}

doc = {
    "agents": {
        "sql_analyst": {
            "system_prompt": "You analyse data.",
            "skills": ["efficiency-discipline"],
            "mcp": ["sql", "skills"],
        }
    }
}
compose_skills(doc, SKILLS, active_tags={"offers"})

prompt = doc["agents"]["sql_analyst"]["system_prompt"]
assert "skills" not in doc["agents"]["sql_analyst"]
assert "### efficiency-discipline" in prompt and "### nbo-analysis" in prompt
assert "- **sql-guide**: Use when writing SQL" in prompt
```

The wording of the two blocks matters. Inlined rules are framed as overriding
the agent's own instructions, so a house rule beats a workflow author's
shortcut. The catalog tells the agent to check *before* starting, to load
*before* doing the work, and to reload a skill that long-run compression has
pushed out of its context — without that last line, agents carry on from a
skill they can no longer see.

## The load tool

Agents load `catalog` and `rag` skills with one tool. Ours is a skills service
behind the MCP gateway, so every agent reaches it as `skills___load_skill` by
holding the `skills` client. A large reference skill answers a bare call with
its table of contents and asks for a query; a call with a query gets the
matching sections from a vector and keyword search.

A project without a skills service can start with a local tool over the same
catalogue:

```python
from strands import tool

LIBRARY = {
    "sql-guide": "## Joins\nPrefer explicit JOIN ... ON.\n\n## Windows\nUse ROW_NUMBER() for top-N.",
}


@tool
def load_skill(name: str, query: str | None = None) -> str:
    """Load a skill's instructions by name.

    Args:
        name: The skill to load, as listed under available skills.
        query: For a long reference skill, the topic to load sections about.
    """
    content = LIBRARY.get(name)
    if content is None:
        return f"No skill named {name!r}. Pass one of: {', '.join(sorted(LIBRARY))}."
    if query is None:
        return content
    sections = [s for s in content.split("\n\n") if query.lower() in s.lower()]
    return "\n\n".join(sections) or f"No section of {name!r} mentions {query!r}."


assert "ROW_NUMBER" in load_skill("sql-guide", "windows")
```

## Prove it

Feed the composer's output to a `Pipeline` with a scripted model, and assert on
the system prompt the model received
([Testing](../testing.md#asserting-what-the-model-was-sent)): the full skill is
inlined, the catalog lists the rest, and nothing named under `skills:` reached
the library.
