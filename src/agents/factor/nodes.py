import json
import logging
from typing import Literal
from pydantic_ai import Agent, RunContext
from langgraph.config import get_stream_writer
from src.common.llm import create_llm_agent
from src.agents.base import *
from src.agents.factor.prompts import *
from src.agents.factor.states import FactorState
from src.tools.rag.retrieve import retrieve_batch
from src.tools.utils.resource_manager import get_resource_manager
from src.tools.utils.formatters import (
    format_web_results_with_prefix,
    format_rag_results_with_prefix,
    format_rag_results,
)
from langgraph.graph import END
from langgraph.types import Command

from src.common.logging import get_logger

logger = get_logger("my_app")
logging.getLogger("httpx").setLevel(logging.WARNING)


def create_factor_task_handler_agent() -> Agent:
    task_handler_agent = create_llm_agent(
        provider="gemini",
        model_name="gemini-2.0-flash",
        system_prompt=factor_task_handler_prompt,
        result_type=TaskHandlerOutput,
        deps_type=TaskHandlerDeps,
    )

    @task_handler_agent.system_prompt
    def add_task_and_feedback(ctx: RunContext[str]):
        return f"Task: {ctx.deps.task}\nFeedback: {ctx.deps.feedback}"

    return task_handler_agent


def create_factor_evaluator_agent() -> Agent:
    evaluator_agent = create_llm_agent(
        provider="gemini",
        model_name="gemini-2.0-flash",
        system_prompt=factor_evaluator_prompt,
        result_type=EvaluatorOutput,
        deps_type=EvaluatorDeps,
    )

    @evaluator_agent.system_prompt
    def add_context(ctx: RunContext[str]):
        context = f"## Original Task: {ctx.deps.task}\n\n## Sub-Queries and Retrieved Contexts:\n"
        for i in range(len(ctx.deps.queries)):
            context += f"{i + 1}. Sub-Query: {ctx.deps.queries[i]}\nRetrieved Context:\n{ctx.deps.contexts[i]}\n\n"

        return context

    return evaluator_agent


def create_factor_extractor_agent() -> Agent:
    extractor_agent = create_llm_agent(
        provider="gemini",
        model_name="gemini-2.0-flash",
        system_prompt=extractor_agent_prompt,
        result_type=ExtractorOutput,
        deps_type=ExtractorDeps,
    )

    @extractor_agent.system_prompt
    def add_context(ctx: RunContext[str]):
        return f"Task: {ctx.deps.task}\nRetrieved Contexts:\n\n{ctx.deps.contexts}"

    return extractor_agent


def create_factor_reflection_agent() -> Agent:
    reflection_agent = create_llm_agent(
        provider="gemini",
        model_name="gemini-2.0-flash",
        system_prompt=reflection_agent_prompt,
        result_type=ReflectionOutput,
        deps_type=ReflectionDeps,
    )

    @reflection_agent.system_prompt
    def add_context(ctx: RunContext[str]):
        return f"Task: {ctx.deps.task}\nExtracted Contexts:\n\n{ctx.deps.extracted_contexts}"

    return reflection_agent


async def task_handler_node(state: FactorState):
    logger.info("Factor Task Handler Node")
    factor_task_handler_agent = create_factor_task_handler_agent()
    res = await factor_task_handler_agent.run(
        "",
        deps=TaskHandlerDeps(
            task=state["factor_task"], feedback=state.get("feedback", "")
        ),
        model_settings={"temperature": 0.0},
    )

    return {"queries": res.output.queries}


async def retriever(
    state: FactorState,
) -> Command[Literal["context_processor_node", END]]:
    logger.info("Factor Retriever Node")
    writer = get_stream_writer()

    rag_results = await retrieve_batch(
        queries=state["queries"],
        collection_name="test",
    )

    factor_evaluator = create_factor_evaluator_agent()
    evaluator_result = await factor_evaluator.run(
        "",
        deps=EvaluatorDeps(
            task=state["factor_task"],
            queries=state["queries"],
            contexts=format_rag_results(rag_results),
        ),
        model_settings={"temperature": 0.0},
    )
    rag_contexts, i, rag_source_map = format_rag_results_with_prefix(
        rag_results,
        len(state.get("rag_source_map", "")) + len(state.get("web_source_map", "")),
        state.get("rag_source_map", {}),
    )

    if evaluator_result.output.should_proceed:
        return Command(
            goto=END,
            update={
                "factor_context": rag_contexts,
                "rag_source_map": rag_source_map,
            },
        )
    else:
        if evaluator_result.output.new_queries:
            search_queries = evaluator_result.output.new_queries
        else:
            search_queries = state["queries"]

        ### Web Search
        web_search_pipeline = get_resource_manager().web_search_pipeline

        web_results = await web_search_pipeline.search_multiple_queries(
            queries=search_queries, max_urls=20, max_results=3
        )

        web_contexts, i, web_source_map = format_web_results_with_prefix(
            web_results, i, state.get("web_source_map", {})
        )

        web_sources = [
            {
                "metadata": {"title": data["title"], "url": source},
                "pageContent": "\n---\n".join(data["contents"]),
            }
            for source, data in web_source_map.items()
        ]

        writer(
            json.dumps(
                {
                    "type": "sources",
                    "data": web_sources,
                    "messageId": state["messageId"],
                }
            )
            + "\n"
        )

        merged_contexts = "\n\n".join([rag_contexts, web_contexts])
        return Command(
            goto="context_processor_node",
            update={
                "raw_contexts": merged_contexts,
                "web_source_map": web_source_map,
                "rag_source_map": rag_source_map,
            },
        )


async def context_processor_node(
    state: FactorState,
) -> Command[Literal["task_handler_node", END]]:
    logger.info("Factor Context Processor Node")
    writer = get_stream_writer()
    extractor_agent = create_factor_extractor_agent()
    extractor_result = await extractor_agent.run(
        "",
        deps=ExtractorDeps(task=state["factor_task"], contexts=state["raw_contexts"]),
        model_settings={"temperature": 0.0},
    )
    extracted_contexts = "\n\n".join(
        [
            f"[{context.reference_number}] {context.title}\nSource: {context.url_or_source}\n{context.content}"
            for context in extractor_result.output.extracted_contexts
        ]
    )

    reflection_agent = create_factor_reflection_agent()

    reflection_result = await reflection_agent.run(
        "",
        deps=ReflectionDeps(
            task=state["factor_task"], extracted_contexts=extracted_contexts
        ),
        model_settings={"temperature": 0.0},
    )

    if reflection_result.output.should_proceed or state.get("loops", 0) > 2:
        return Command(
            goto=END,
            update={"factor_context": extracted_contexts, "loops": 0},
        )
    else:
        return Command(
            goto="task_handler_node",
            update={
                "feedback": reflection_result.output.feedback_to_task_handler,
                "loops": state.get("loops", 0) + 1,
            },
        )
