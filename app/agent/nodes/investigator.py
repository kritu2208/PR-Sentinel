"""
Investigator Node for PR Sentinel.
Conducts autonomous root cause analysis and impact evaluation for validated findings.
"""
import logging
import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.agent.prompts import INVESTIGATOR_SYSTEM_PROMPT, build_investigator_user_prompt
from app.agent.schemas import InvestigatorOutput
from app.agent.state import ReviewState
from app.config import settings

logger = logging.getLogger("pr-sentinel.investigator")


async def investigator_node(state: ReviewState) -> dict:
    """
    Autonomous investigation node:
    1. Receives only validated findings from validator_node.
    2. Identifies meaningful findings that warrant root-cause investigation
       (e.g., severity in critical/high/medium and confidence >= MIN_CONFIDENCE_THRESHOLD).
    3. Leverages existing retrieved_context and changed_files diffs without second retrieval.
    4. Invokes LLM with structured InvestigatorOutput schema.
    5. Enriches findings with root_cause, evidence, impact, recommendation, and investigation_status.
    6. Fails safely on LLM errors without corrupting the review pipeline.
    """
    validated_findings = state.get("validated_findings") or []
    if not validated_findings:
        logger.debug("No validated findings to investigate; skipping investigator.")
        return {
            "investigated_findings": [],
            "validated_findings": [],
        }

    # Selective triage: investigate actionable findings; skip trivial or low-confidence findings
    min_confidence = getattr(settings, "MIN_CONFIDENCE_THRESHOLD", 0.7)
    investigable_findings = []
    uninvestigated_findings = []

    for f in validated_findings:
        sev = f.get("severity", "low").lower()
        conf = float(f.get("confidence", 1.0))
        # Investigate critical, high, and medium severity findings meeting confidence threshold
        if sev in ("critical", "high", "medium") and conf >= min_confidence:
            investigable_findings.append(f)
        else:
            uninvestigated_findings.append({
                **f,
                "investigation_status": "skipped",
            })

    if not investigable_findings:
        logger.info("All validated findings were low-severity or low-confidence; skipping LLM investigation.")
        return {
            "investigated_findings": uninvestigated_findings,
            "validated_findings": uninvestigated_findings,
        }

    # Validate API key presence
    if not settings.GROQ_API_KEY:
        logger.warning("GROQ_API_KEY is not configured; skipping LLM root-cause investigation.")
        skipped_findings = [{**f, "investigation_status": "skipped"} for f in validated_findings]
        return {
            "investigated_findings": skipped_findings,
            "validated_findings": skipped_findings,
        }

    changed_files = state.get("changed_files") or []
    retrieved_context = state.get("retrieved_context") or []
    pr_title = state.get("pr_title", "Pull Request")

    user_prompt = build_investigator_user_prompt(
        pr_title=pr_title,
        findings=investigable_findings,
        changed_files=changed_files,
        retrieved_context=retrieved_context,
    )

    messages = [
        SystemMessage(content=INVESTIGATOR_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    try:
        llm = ChatGroq(
            groq_api_key=settings.GROQ_API_KEY,
            model_name=settings.GROQ_MODEL,
            temperature=0.1,
        )
        structured_llm = llm.with_structured_output(InvestigatorOutput)
        response = await structured_llm.ainvoke(messages)

        investigation_map: dict[tuple[str, int | None, str], dict] = {}
        file_line_map: dict[tuple[str, int | None], dict] = {}
        file_title_map: dict[tuple[str, str], dict] = {}

        if isinstance(response, InvestigatorOutput):
            items = response.investigations
        elif isinstance(response, dict):
            parsed = InvestigatorOutput.model_validate(response)
            items = parsed.investigations
        else:
            items = []

        for item in items:
            item_dict = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            key = (item_dict.get("file", ""), item_dict.get("line"), item_dict.get("title", ""))
            investigation_map[key] = item_dict
            if item_dict.get("file"):
                file_line_map[(item_dict["file"], item_dict.get("line"))] = item_dict
                file_title_map[(item_dict["file"], item_dict.get("title", ""))] = item_dict

        enriched_investigated = []
        for f in investigable_findings:
            key = (f.get("file", ""), f.get("line"), f.get("title", ""))
            inv = investigation_map.get(key)
            if not inv:
                inv = file_line_map.get((f.get("file", ""), f.get("line")))
            if not inv:
                inv = file_title_map.get((f.get("file", ""), f.get("title", "")))

            if inv:
                root_cause = inv.get("root_cause")
                evidence = inv.get("evidence")
                impact = inv.get("impact")
                recommendation = inv.get("recommendation")
                status = inv.get("status", "confirmed")
                inv_conf = float(inv.get("confidence", 1.0))

                enriched_finding = {
                    **f,
                    "root_cause": root_cause,
                    "evidence": evidence,
                    "impact": impact,
                    "recommendation": recommendation,
                    "investigation_status": status,
                    "confidence": min(float(f.get("confidence", 1.0)), inv_conf) if status == "uncertain" else float(f.get("confidence", 1.0)),
                }
                enriched_investigated.append(enriched_finding)
            else:
                enriched_investigated.append({
                    **f,
                    "investigation_status": "uncertain",
                })

        all_enriched = enriched_investigated + uninvestigated_findings
        logger.info(
            "Investigator enriched %s finding(s) with root-cause analysis (%s confirmed)",
            len(enriched_investigated),
            sum(1 for f in enriched_investigated if f.get("investigation_status") == "confirmed"),
        )
        return {
            "investigated_findings": all_enriched,
            "validated_findings": all_enriched,
        }

    except Exception as exc:
        logger.error(
            "Error during Investigator LLM invocation: %s: %s (falling back safely)",
            exc.__class__.__name__,
            str(exc),
        )
        # Fail safe: mark as failed but preserve all findings intact
        fallback_findings = [
            {**f, "investigation_status": "failed"} for f in validated_findings
        ]
        return {
            "investigated_findings": fallback_findings,
            "validated_findings": fallback_findings,
        }
