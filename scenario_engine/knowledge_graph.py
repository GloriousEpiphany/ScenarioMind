"""Local auditable property graph for Scenario Lab V2.

The graph is intentionally backed by deterministic template data and user
records. It implements local multi-hop property graph retrieval and does not
claim external GraphRAG/vector retrieval when no such connector is configured.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from .schemas import Evidence, GraphImportReport, GraphSearchResult, GraphTextDocument, KnowledgeEdge, KnowledgeNode, ScenarioSpec
from .templates import SCENARIO_TEMPLATES


class LocalKnowledgeGraph:
    ALLOWED_NODE_TYPES = {"event_template", "risk_entity", "historical_window", "industry", "supply_chain", "shareholder", "policy", "theme", "asset_proxy"}
    ALLOWED_RELATIONS = {"affects", "has_historical_analog", "supplies", "owns", "regulated_by", "maps_to_theme", "proxies"}

    def __init__(self):
        self.nodes: dict[str, KnowledgeNode] = {}
        self.edges: dict[str, KnowledgeEdge] = {}
        self.documents: dict[str, GraphTextDocument] = {}
        self._seed_from_templates()

    def search(self, query: str, top_k: int = 10, hop_depth: int = 3) -> GraphSearchResult:
        if hop_depth < 1 or hop_depth > 5:
            raise ValueError("hop_depth must be between 1 and 5")
        tokens = self._tokens(query)
        scored_nodes = []
        for node in self.nodes.values():
            score = self._score(tokens, f"{node.name} {node.description} {' '.join(map(str, node.metadata.values()))}")
            if score > 0:
                scored_nodes.append((score, node))
        scored_nodes.sort(key=lambda item: item[0], reverse=True)
        seed_nodes = [item[1] for item in scored_nodes[:top_k]]
        node_ids = {node.node_id for node in seed_nodes}
        expanded_node_ids, matched_edges, paths = self._expand_paths(node_ids, hop_depth)
        matched_nodes = [self.nodes[node_id] for node_id in expanded_node_ids if node_id in self.nodes]
        matched_documents = self._search_documents(tokens, top_k=top_k)
        evidence = [
            Evidence(
                evidence_id="evidence_local_knowledge_graph",
                evidence_type="knowledge_graph",
                title="本地情景知识图谱",
                content="检索结果来自本地可审计 Property Graph 的节点、边、多跳路径扩展和文本证据召回。",
                confidence=0.75,
                source_uri="local://scenario_engine/templates.py",
                metrics={
                    "matched_nodes": len(matched_nodes),
                    "matched_edges": len(matched_edges),
                    "matched_documents": len(matched_documents),
                    "matched_document_ids": [item.document_id for item in matched_documents],
                    "path_count": len(paths),
                    "paths": paths[:20],
                },
            )
        ]
        for doc in matched_documents:
            evidence.append(
                Evidence(
                    evidence_id=f"evidence_graph_doc_{doc.document_id}",
                    evidence_type="graph_text_document",
                    title=doc.title,
                    content=doc.content[:500],
                    confidence=0.62,
                    source_uri=doc.source_uri,
                    metrics={"document_id": doc.document_id, **doc.metadata},
                )
            )
        return GraphSearchResult(
            query=query,
            matched_nodes=matched_nodes,
            matched_edges=matched_edges,
            evidence=evidence,
            audit={
                "method": "local_graphrag_property_graph_and_text_retrieval",
                "top_k": top_k,
                "external_graph_used": False,
                "vector_store": "local_token_retrieval",
                "hop_depth": hop_depth,
                "retrieval_layers": ["property_graph", "multi_hop_paths", "local_text_documents"],
                "path_count": len(paths),
                "validation_status": "passed",
            },
        )

    def enrich_spec(self, spec: ScenarioSpec) -> tuple[list[KnowledgeEdge], list[Evidence]]:
        result = self.search(" ".join([spec.event_name, *spec.affected_entities]), top_k=20, hop_depth=3)
        return result.matched_edges, result.evidence

    def import_relationships(self, items: list[dict], source_uri: str, operator: str) -> GraphImportReport:
        if not source_uri.strip():
            raise ValueError("source_uri is required for graph imports")
        imported_nodes: list[KnowledgeNode] = []
        imported_edges: list[KnowledgeEdge] = []
        rejected: list[dict] = []
        for index, item in enumerate(items):
            try:
                source = self._validated_node(item["source"], source_uri)
                target = self._validated_node(item["target"], source_uri)
                relation_type = item["relation_type"]
                if relation_type not in self.ALLOWED_RELATIONS:
                    raise ValueError(f"unsupported relation_type: {relation_type}")
                self.nodes[source.node_id] = source
                self.nodes[target.node_id] = target
                edge = self._build_edge(source.node_id, target.node_id, relation_type, item.get("description") or f"{source.name} {relation_type} {target.name}", source_uri)
                self.edges[edge.edge_id] = edge
                imported_nodes.extend([source, target])
                imported_edges.append(edge)
            except Exception as exc:
                rejected.append({"index": index, "reason": str(exc), "item": item})
        import_id = "graph_import_" + hashlib.sha1(f"{source_uri}|{operator}|{repr(items)}".encode("utf-8")).hexdigest()[:12]
        return GraphImportReport(
            import_id=import_id,
            imported_nodes=imported_nodes,
            imported_edges=imported_edges,
            rejected_items=rejected,
            audit={
                "method": "validated_manual_graph_import",
                "source_uri": source_uri,
                "operator": operator,
                "validation_status": "passed" if not rejected else "partial",
            },
        )

    def apply_import_report(self, report_json: dict) -> None:
        for raw_node in report_json.get("imported_nodes", []):
            node = KnowledgeNode(**raw_node)
            self.nodes[node.node_id] = node
        for raw_edge in report_json.get("imported_edges", []):
            edge = KnowledgeEdge(**raw_edge)
            self.edges[edge.edge_id] = edge

    def ingest_text_document(self, title: str, content: str, source_uri: str, metadata: dict | None = None) -> tuple[GraphTextDocument, GraphImportReport]:
        if not title.strip() or not content.strip() or not source_uri.strip():
            raise ValueError("title, content and source_uri are required")
        document_id = hashlib.sha1(f"{source_uri}|{title}|{content}".encode("utf-8")).hexdigest()[:12]
        document = GraphTextDocument(
            document_id=document_id,
            title=title.strip(),
            content=content.strip(),
            source_uri=source_uri.strip(),
            created_at=datetime.now().isoformat(timespec="seconds"),
            metadata=metadata or {},
        )
        self.documents[document.document_id] = document
        causal_items = self.extract_causal_relationships(document.content, document.source_uri)
        report = self.import_relationships(causal_items, source_uri=document.source_uri, operator=str((metadata or {}).get("operator", "system"))) if causal_items else GraphImportReport(
            import_id=f"graph_import_{document.document_id}",
            imported_nodes=[],
            imported_edges=[],
            rejected_items=[],
            audit={
                "method": "rule_based_financial_event_causality_extraction",
                "source_uri": document.source_uri,
                "validation_status": "passed",
                "extracted_edges": 0,
            },
        )
        report.audit["document_id"] = document.document_id
        return document, report

    def apply_text_document(self, raw: dict) -> None:
        document = GraphTextDocument(**raw)
        self.documents[document.document_id] = document

    def extract_causal_relationships(self, content: str, source_uri: str) -> list[dict]:
        items = []
        separators = ["导致", "影响", "推升", "压制", "拖累", "利好", "利空"]
        for sep in separators:
            if sep not in content:
                continue
            left, right = content.split(sep, 1)
            cause = self._clean_phrase(left[-24:])
            effect = self._clean_phrase(right[:24])
            if not cause or not effect:
                continue
            items.append(
                {
                    "source": {
                        "node_type": "theme",
                        "name": cause,
                        "description": "从文本中规则抽取的原因节点，需通过来源与后续回测校验。",
                        "metadata": {"extraction": "rule_based", "source_uri": source_uri},
                    },
                    "target": {
                        "node_type": "theme",
                        "name": effect,
                        "description": "从文本中规则抽取的结果节点，需通过来源与后续回测校验。",
                        "metadata": {"extraction": "rule_based", "source_uri": source_uri},
                    },
                    "relation_type": "affects",
                    "description": f"{cause} {sep} {effect}",
                }
            )
        return items[:10]

    def _seed_from_templates(self) -> None:
        for template in SCENARIO_TEMPLATES:
            event_id = self._node_id("event", template["name"])
            self.nodes[event_id] = KnowledgeNode(
                node_id=event_id,
                node_type="event_template",
                name=template["name"],
                description=f"{template['event_type']} / {template['region']}",
                source="template",
                confidence=0.78,
                metadata={"keywords": template["keywords"], "missing": template["missing"]},
            )
            for entity in template["affected_entities"]:
                entity_id = self._node_id("entity", entity)
                self.nodes.setdefault(
                    entity_id,
                    KnowledgeNode(
                        node_id=entity_id,
                        node_type="risk_entity",
                        name=entity,
                        description="由内置情景模板声明的受影响对象。",
                        source="template",
                        confidence=0.65,
                    ),
                )
                self._add_edge(event_id, entity_id, "affects", f"{template['name']} 影响 {entity}")
            for start_date, end_date, title in template["historical_windows"]:
                window_id = self._node_id("historical_window", title)
                self.nodes[window_id] = KnowledgeNode(
                    node_id=window_id,
                    node_type="historical_window",
                    name=title,
                    description=f"{start_date} 至 {end_date}",
                    source="template",
                    confidence=0.72,
                    metadata={"start_date": start_date, "end_date": end_date},
                )
                self._add_edge(event_id, window_id, "has_historical_analog", f"{template['name']} 的历史类比窗口：{title}")

    def _add_edge(self, source_id: str, target_id: str, relation_type: str, description: str) -> None:
        edge = self._build_edge(source_id, target_id, relation_type, description, "local://scenario_engine/templates.py")
        self.edges[edge.edge_id] = edge

    def _build_edge(self, source_id: str, target_id: str, relation_type: str, description: str, source_uri: str) -> KnowledgeEdge:
        digest = hashlib.sha1(f"{source_id}|{target_id}|{relation_type}|{source_uri}".encode("utf-8")).hexdigest()[:12]
        return KnowledgeEdge(
            edge_id=f"edge_{digest}",
            source_node_id=source_id,
            target_node_id=target_id,
            relation_type=relation_type,
            description=description,
            evidence_id="evidence_local_knowledge_graph" if source_uri.startswith("local://") else f"evidence_graph_import_{digest}",
            confidence=0.66 if source_uri.startswith("local://") else 0.55,
            metadata={"source_uri": source_uri},
        )

    def _expand_paths(self, seed_node_ids: set[str], hop_depth: int) -> tuple[set[str], list[KnowledgeEdge], list[list[str]]]:
        expanded = set(seed_node_ids)
        edges_by_id: dict[str, KnowledgeEdge] = {}
        paths: list[list[str]] = []
        frontier = [(node_id, [node_id]) for node_id in seed_node_ids]
        for _ in range(hop_depth):
            next_frontier = []
            for node_id, path in frontier:
                for edge in self.edges.values():
                    neighbor = None
                    if edge.source_node_id == node_id:
                        neighbor = edge.target_node_id
                    elif edge.target_node_id == node_id:
                        neighbor = edge.source_node_id
                    if neighbor is None:
                        continue
                    edges_by_id[edge.edge_id] = edge
                    if neighbor in path:
                        continue
                    next_path = [*path, neighbor]
                    paths.append([self.nodes[item].name for item in next_path if item in self.nodes])
                    if neighbor not in expanded:
                        expanded.add(neighbor)
                        next_frontier.append((neighbor, next_path))
            frontier = next_frontier
            if not frontier:
                break
        return expanded, list(edges_by_id.values()), paths

    def _validated_node(self, raw: dict, source_uri: str) -> KnowledgeNode:
        node_type = raw.get("node_type")
        name = (raw.get("name") or "").strip()
        if node_type not in self.ALLOWED_NODE_TYPES:
            raise ValueError(f"unsupported node_type: {node_type}")
        if not name:
            raise ValueError("node name is required")
        return KnowledgeNode(
            node_id=self._node_id(node_type, name),
            node_type=node_type,
            name=name,
            description=raw.get("description") or "手工导入的可审计图谱节点。",
            source=source_uri,
            confidence=float(raw.get("confidence", 0.55)),
            metadata=raw.get("metadata") or {},
        )

    def _node_id(self, node_type: str, name: str) -> str:
        digest = hashlib.sha1(f"{node_type}|{name}".encode("utf-8")).hexdigest()[:12]
        return f"node_{digest}"

    def _tokens(self, value: str) -> set[str]:
        text = value.lower()
        tokens = {text[i : i + 2] for i in range(max(len(text) - 1, 0))}
        tokens.update(part for part in text.replace(",", " ").replace("，", " ").split() if part)
        return tokens

    def _search_documents(self, tokens: set[str], top_k: int) -> list[GraphTextDocument]:
        scored = []
        for document in self.documents.values():
            score = self._score(tokens, f"{document.title} {document.content}")
            if score > 0:
                scored.append((score, document))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:top_k]]

    def _clean_phrase(self, value: str) -> str:
        cleaned = value.replace("。", " ").replace("，", " ").replace(",", " ").replace("：", " ").strip()
        parts = [part for part in cleaned.split() if part]
        if not parts:
            return ""
        return parts[-1][:24]

    def _score(self, tokens: set[str], text: str) -> float:
        haystack = text.lower()
        if not tokens:
            return 0.0
        hits = sum(1 for token in tokens if token and token in haystack)
        return hits / len(tokens)
