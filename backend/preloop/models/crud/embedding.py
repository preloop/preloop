"""CRUD operations for EmbeddingModel and IssueEmbedding models."""

import json
from datetime import datetime, UTC
from typing import Dict, List, Optional, Tuple, Union

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.comment import Comment
from ..models.issue import Issue, IssueEmbedding, EmbeddingModel
from ..models.organization import Organization
from ..models.project import Project
from ..models.tracker import Tracker
from .base import CRUDBase

from ..db.vector_types import TRUNCATED_VECTOR_SIZE


class CRUDEmbeddingModel(CRUDBase[EmbeddingModel]):
    """CRUD operations for EmbeddingModel model."""

    def get_by_name(self, db: Session, *, name: str) -> Optional[EmbeddingModel]:
        """Get embedding model by name."""
        return db.query(EmbeddingModel).filter(EmbeddingModel.name == name).first()

    def get_by_provider_version(
        self, db: Session, *, provider: str, version: str
    ) -> Optional[EmbeddingModel]:
        """Get embedding model by provider and version."""
        return (
            db.query(EmbeddingModel)
            .filter(
                EmbeddingModel.provider == provider, EmbeddingModel.version == version
            )
            .first()
        )

    def get_active(self, db: Session) -> List[EmbeddingModel]:
        """Get all active embedding models."""
        return db.query(EmbeddingModel).filter(EmbeddingModel.is_active.is_(True)).all()


class CRUDIssueEmbedding(CRUDBase[IssueEmbedding]):
    """CRUD operations for IssueEmbedding model."""

    def get_for_issue(
        self, db: Session, *, issue_id: str, account_id: Optional[str] = None
    ) -> Dict[str, IssueEmbedding]:
        """Get all embeddings for an issue (including its content and all its comments), keyed by model name."""
        query = (
            db.query(IssueEmbedding, EmbeddingModel)
            .join(EmbeddingModel)
            .filter(IssueEmbedding.issue_id == issue_id)
        )
        if account_id:
            query = (
                query.join(Issue, IssueEmbedding.issue_id == Issue.id)
                .join(Tracker, Issue.tracker_id == Tracker.id)
                .filter(Tracker.account_id == account_id)
            )
        embeddings = query.all()
        return {model.name: embedding for embedding, model in embeddings}

    def get_for_issue_content(
        self, db: Session, *, issue_id: str, account_id: Optional[str] = None
    ) -> Dict[str, IssueEmbedding]:
        """Get embeddings specifically for an issue's main content (not comments), keyed by model name."""
        query = (
            db.query(IssueEmbedding, EmbeddingModel)
            .join(EmbeddingModel)
            .filter(
                IssueEmbedding.issue_id == issue_id, IssueEmbedding.comment_id.is_(None)
            )
        )
        if account_id:
            query = (
                query.join(Issue, IssueEmbedding.issue_id == Issue.id)
                .join(Tracker, Issue.tracker_id == Tracker.id)
                .filter(Tracker.account_id == account_id)
            )
        embeddings = query.all()
        return {model.name: embedding for embedding, model in embeddings}

    def get_for_comment(
        self, db: Session, *, comment_id: str, account_id: Optional[str] = None
    ) -> Dict[str, IssueEmbedding]:
        """Get all embeddings for a specific comment, keyed by model name."""
        query = (
            db.query(IssueEmbedding, EmbeddingModel)
            .join(EmbeddingModel)
            .filter(IssueEmbedding.comment_id == comment_id)
        )
        if account_id:
            query = (
                query.join(Issue, IssueEmbedding.issue_id == Issue.id)
                .join(Tracker, Issue.tracker_id == Tracker.id)
                .filter(Tracker.account_id == account_id)
            )
        embeddings = query.all()
        return {model.name: embedding for embedding, model in embeddings}

    def get_for_model(
        self,
        db: Session,
        *,
        model_id: str,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[IssueEmbedding]:
        """Get embeddings for a specific model."""
        query = db.query(IssueEmbedding).filter(
            IssueEmbedding.embedding_model_id == model_id
        )
        if account_id:
            query = (
                query.join(Issue, IssueEmbedding.issue_id == Issue.id)
                .join(Tracker, Issue.tracker_id == Tracker.id)
                .filter(Tracker.account_id == account_id)
            )
        return query.offset(skip).limit(limit).all()

    def get_raw_embeddings(
        self,
        db: Session,
        *,
        embedding_model_id: Optional[str] = None,
        project_ids: Optional[List[str]] = None,
        project_names: Optional[List[str]] = None,
        tracker_id: Optional[str] = None,
        organization_ids: Optional[List[str]] = None,
        organization_names: Optional[List[str]] = None,
        account_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 1000,  # Default to a higher limit for raw data
    ) -> List[Tuple[str, List[float], str, Optional[str], Optional[datetime]]]:
        """
        Get raw embedding vectors for issues, with optional filtering.
        Returns a list of (issue_id, embedding_vector, issue_title, issue_type, issue_last_updated_external) tuples.
        """
        params = {
            "model_id": embedding_model_id,
            "query_vector": None,  # Not used in this function
            "limit": limit,
            "skip": skip,
        }
        if embedding_model_id:
            params["embedding_model_id"] = embedding_model_id

        if project_ids:
            params["project_ids"] = project_ids
        if project_names:
            lowercase_project_names = [name.lower() for name in project_names]
            params["project_names"] = lowercase_project_names
        if tracker_id:
            params["tracker_id"] = tracker_id
        if organization_ids:
            params["organization_ids"] = organization_ids
        if organization_names:
            lowercase_org_names = [name.lower() for name in organization_names]
            params["organization_names"] = lowercase_org_names
        if account_id:
            params["account_id"] = account_id

        query = db.query(
            IssueEmbedding.issue_id,
            IssueEmbedding.embedding,
            Issue.title,
            Issue.issue_type,
            Issue.last_updated_external,
        ).join(Issue, IssueEmbedding.issue_id == Issue.id)

        if embedding_model_id:
            query = query.filter(
                IssueEmbedding.embedding_model_id == embedding_model_id
            )

        if project_ids:
            query = query.filter(Issue.project_id.in_(project_ids))

        if project_names:
            query = query.join(Project, Issue.project_id == Project.id).filter(
                func.lower(Project.name).in_(lowercase_project_names)
            )

        if tracker_id:
            query = query.filter(Issue.tracker_id == tracker_id)

        if organization_ids:
            # Join with Project table to filter by organization_id
            query = query.join(Project, Issue.project_id == Project.id)
            query = query.filter(Project.organization_id.in_(organization_ids))

        if organization_names:
            query = (
                query.join(Project, Issue.project_id == Project.id)
                .join(Organization, Project.organization_id == Organization.id)
                .filter(func.lower(Organization.name).in_(lowercase_org_names))
            )

        if account_id:
            query = query.join(Tracker, Issue.tracker_id == Tracker.id)
            query = query.filter(Tracker.account_id == account_id)

        return query.offset(skip).limit(limit).all()

    def create_embeddings(
        self,
        db: Session,
        *,
        issue_id: str,
        comment_id: Optional[str] = None,
        force_update: bool = False,
        api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Create embeddings for an issue's content or a specific comment using all active embedding models.

        This implementation supports real embedding generation with OpenAI or other providers
        when an API key is provided, or falls back to random vectors for testing.

        Args:
            db: Database session
            issue_id: ID of the issue to associate with the embedding
            comment_id: Optional ID of the comment to create embeddings for. If None, creates for issue content.
            force_update: Whether to update existing embeddings
            api_key: Optional API key for embedding providers

        Returns:
            Dictionary mapping model names to status ("created", "updated", "already_exists", "error")
        """
        text_to_embed: str
        source_entity_description: str

        if comment_id:
            comment = db.get(Comment, comment_id)
            if not comment:
                raise ValueError(f"Comment with ID {comment_id} not found")
            if comment.issue_id != issue_id:
                # Or handle as a different kind of error, depending on desired strictness
                raise ValueError(
                    f"Comment {comment_id} does not belong to issue {issue_id}"
                )
            text_to_embed = comment.body
            source_entity_description = f"comment {comment_id}"
        else:
            issue = db.get(Issue, issue_id)
            if not issue:
                raise ValueError(f"Issue with ID {issue_id} not found")
            text_to_embed = f"{issue.title}: {issue.description or ''}"
            source_entity_description = f"issue {issue_id} content"

        if not text_to_embed.strip():
            return {
                model.name: "skipped_empty_text"
                for model in db.query(EmbeddingModel)
                .filter(EmbeddingModel.is_active.is_(True))
                .all()
            }

        # Get active embedding models
        embedding_models = (
            db.query(EmbeddingModel).filter(EmbeddingModel.is_active.is_(True)).all()
        )

        results = {}
        for model in embedding_models:
            # Check if embedding already exists
            query = db.query(IssueEmbedding).filter(
                IssueEmbedding.issue_id == issue_id,
                IssueEmbedding.embedding_model_id == model.id,
            )
            if comment_id:
                query = query.filter(IssueEmbedding.comment_id == comment_id)
            else:
                query = query.filter(IssueEmbedding.comment_id.is_(None))

            existing = query.first()

            if existing and not force_update:
                results[model.name] = f"already_exists_for_{source_entity_description}"
                continue

            # Generate embedding vector
            try:
                embedding_vector = self._generate_embedding_vector(
                    text=text_to_embed, model=model, api_key=api_key
                )

                meta_data = {
                    "source": source_entity_description,
                    "text_processed": text_to_embed[:100] + "..."
                    if len(text_to_embed) > 100
                    else text_to_embed,
                }

                # Create or update embedding
                if existing:
                    existing.embedding = embedding_vector
                    existing.meta_data = {
                        "updated_at": datetime.now(UTC).isoformat(),
                        **meta_data,
                    }
                    db.add(existing)
                    results[model.name] = f"updated_for_{source_entity_description}"
                else:
                    new_embedding = IssueEmbedding(
                        id=self.model.generate_id(),
                        issue_id=issue_id,
                        comment_id=comment_id,  # Pass comment_id
                        embedding_model_id=model.id,
                        embedding=embedding_vector,
                        meta_data=meta_data,
                    )
                    # Flush inside a savepoint so a concurrent insert (e.g. two
                    # webhook deliveries for the same comment racing through the
                    # check-then-insert above) surfaces here as an IntegrityError
                    # we can recover from, instead of blowing up the whole
                    # transaction at commit time (uix_issue_embedding_model).
                    try:
                        with db.begin_nested():
                            db.add(new_embedding)
                            db.flush()
                        results[model.name] = f"created_for_{source_entity_description}"
                    except IntegrityError:
                        # Row was inserted concurrently; update it instead.
                        concurrent = query.first()
                        if concurrent is not None:
                            concurrent.embedding = embedding_vector
                            concurrent.meta_data = {
                                "updated_at": datetime.now(UTC).isoformat(),
                                **meta_data,
                            }
                            db.add(concurrent)
                            results[model.name] = (
                                f"updated_for_{source_entity_description}"
                            )
                        else:
                            results[model.name] = (
                                f"already_exists_for_{source_entity_description}"
                            )
            except Exception as e:
                results[model.name] = f"error_for_{source_entity_description}: {str(e)}"

        db.commit()
        return results

    def _generate_embedding_vector(
        self, text: str, model: EmbeddingModel, api_key: Optional[str] = None
    ) -> List[float]:
        """
        Generate an embedding vector for the given text using the specified model.

        This implementation supports:
        - OpenAI embedding models
        - HuggingFace transformer models
        - Fallback to random vectors for testing

        Args:
            text: Text to embed
            model: EmbeddingModel with provider and version information
            api_key: Optional API key for the provider

        Returns:
            Embedding vector as a list of floats
        """
        provider = model.provider.lower()
        version = model.version

        # Real embedding generation based on provider
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=api_key)

            # Generate embedding
            response = client.embeddings.create(
                model=version,  # e.g., "text-embedding-ada-002"
                input=text,
            )

            # Extract embedding
            embedding = response.data[0].embedding

            return embedding

        elif provider == "huggingface":
            from sentence_transformers import SentenceTransformer

            # Load model
            model = SentenceTransformer(version)

            # Generate embedding
            embedding = model.encode(text).tolist()

            return embedding

        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def similarity_search(
        self,
        db: Session,
        *,
        model_id: str,
        query_vector: List[float],
        limit: int = 10,
        skip: int = 0,
        similarity: Optional[float] = None,
        distance_type: str = "cosine",  # Note: distance_type is not used in this SQL version
        tracker_ids: Optional[List[str]] = None,
        project_ids: Optional[List[str]] = None,
        status: Optional[str] = None,
        labels: Optional[List[str]] = None,
        priority: Optional[str] = None,
        assignee: Optional[str] = None,
        last_updated_before: Optional[datetime] = None,
        last_updated_after: Optional[datetime] = None,
        embedding_type: Optional[str] = None,  # "issue", "comment", or None
        account_id: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> List[Union[Tuple[Issue, float], Tuple[Comment, float]]]:
        """
        Search for similar issues or comments based on vector similarity using raw SQL with pgvector.

        Args:
            db: Database session
            model_id: ID of the embedding model to search within
            query_vector: Vector to search for
            limit: Maximum number of results to return
            skip: Number of results to skip before returning
            similarity: Optional similarity score to filter by. If provided, only results with
                        a similarity score greater than or equal to this value will be returned.
            distance_type: (Currently unused in this SQL implementation) Distance metric.
            tracker_ids: Optional list of tracker IDs to filter by (applies to the issue).
            project_ids: Optional list of project IDs to filter by (applies to the issue).
            status: Optional status to filter by (applies to the issue).
            labels: Optional list of labels to filter by (applies to the issue, must contain all specified labels).
            priority: Optional priority to filter by (applies to the issue).
            assignee: Optional assignee (stored in issue's meta_data->>'assignee') to filter by.
            last_updated_before: Optional upper bound for issue updated_at.
            last_updated_after: Optional lower bound for issue updated_at.
            embedding_type: Optional type of embedding to filter by.
                            Can be "issue", "comment". If None, results can be a mix of Issues and Comments.
            account_id: Optional account ID to filter by.
            sort: Optional sort order. 'newest' sorts by creation date descending.

        Returns:
            List of (Issue, similarity_score) tuples if embedding_type is "issue".
            List of (Comment, similarity_score) tuples if embedding_type is "comment".
            List containing a mix of (Issue, similarity_score) and (Comment, similarity_score)
            tuples if embedding_type is None, ordered by similarity.
        """
        params = {
            "model_id": model_id,
            "query_vector": query_vector,  # pgvector expects a string representation of a list
            "limit": limit,
            "skip": skip,
        }
        if similarity is not None:
            params["similarity"] = similarity

        # Determine the sorting order
        order_by_sql = "sim DESC"
        if sort == "newest":
            # This alias is defined in each of the subqueries below
            order_by_sql = "updated_at DESC"

        common_where_clauses = ["e.embedding_model_id = :model_id"]

        if tracker_ids:
            common_where_clauses.append("i.tracker_id = ANY(:tracker_ids)")
            params["tracker_ids"] = tracker_ids
        if project_ids:
            common_where_clauses.append("i.project_id = ANY(:project_ids)")
            params["project_ids"] = project_ids
        if status and status != "all":
            common_where_clauses.append("i.status = :status")
            params["status"] = status
        if priority:
            common_where_clauses.append("i.priority = :priority")
            params["priority"] = priority
        if labels:
            common_where_clauses.append(
                "i.meta_data->'labels' @> CAST(:labels AS JSONB)"
            )
            params["labels"] = json.dumps(labels)
        if assignee:
            common_where_clauses.append("i.meta_data->>'assignee' = :assignee")
            params["assignee"] = assignee
        if last_updated_after:
            common_where_clauses.append("i.updated_at > :last_updated_after")
            params["last_updated_after"] = last_updated_after
        if last_updated_before:
            common_where_clauses.append("i.updated_at < :last_updated_before")
            params["last_updated_before"] = last_updated_before
        if account_id:
            common_where_clauses.append("t.account_id = :account_id")
            params["account_id"] = account_id

        processed_results: List[Union[Tuple[Issue, float], Tuple[Comment, float]]] = []

        if embedding_type == "issue":
            specific_where_clauses = common_where_clauses + [
                "e.issue_id IS NOT NULL",
                "e.comment_id IS NULL",
            ]
            if similarity is not None:
                specific_where_clause = "sim_trunc >= :similarity"
            else:
                specific_where_clause = "sim_trunc >= 0"

            where_sql = " AND ".join(specific_where_clauses)
            sql = f"""
                WITH similarity_calc AS (
                    SELECT
                        i.id, i.title, i.description, i.status, i.priority,
                        i.issue_type, i.external_id, i.external_url, i.key,
                        i.project_id, i.tracker_id, i.meta_data AS issue_meta_data,
                        i.last_updated_external, i.last_synced,
                        i.created_at AS issue_created_at,
                        i.updated_at AS updated_at, -- Alias for sorting
                        e.embedding AS embedding,
                        (1 - (subvector(embedding, 1, {TRUNCATED_VECTOR_SIZE})::vector({TRUNCATED_VECTOR_SIZE}) <=> (select subvector(CAST(:query_vector AS vector), 1, {TRUNCATED_VECTOR_SIZE})))) as sim_trunc
                    FROM
                        issueembedding e
                    JOIN
                        issue i ON e.issue_id = i.id
                    JOIN
                        tracker t ON i.tracker_id = t.id
                    WHERE {where_sql}
                ),
                shortlist AS (
                    SELECT * FROM similarity_calc
                    WHERE {specific_where_clause}
                    ORDER BY sim_trunc DESC
                    LIMIT :limit * 2
                )
                SELECT
                    id, title, description, status, priority,
                    issue_type, external_id, external_url, key,
                    project_id, tracker_id, issue_meta_data,
                    last_updated_external, last_synced,
                    issue_created_at,
                    updated_at,
                    (1 - (embedding <=> (select CAST(:query_vector AS vector)))) as sim
                FROM shortlist
                ORDER BY {order_by_sql}
                LIMIT :limit OFFSET :skip
            """
            query_results = db.execute(text(sql), params).fetchall()
            for row in query_results:
                issue = Issue(
                    id=row.id,
                    title=row.title,
                    description=row.description,
                    status=row.status,
                    priority=row.priority,
                    issue_type=row.issue_type,
                    external_id=row.external_id,
                    external_url=row.external_url,
                    key=row.key,
                    project_id=row.project_id,
                    tracker_id=row.tracker_id,
                    meta_data=json.loads(row.issue_meta_data)
                    if row.issue_meta_data and isinstance(row.issue_meta_data, str)
                    else row.issue_meta_data,
                    last_updated_external=row.last_updated_external,
                    last_synced=row.last_synced,
                    created_at=row.issue_created_at,
                    updated_at=row.updated_at,
                )
                processed_results.append((issue, row.sim))

        elif embedding_type == "comment":
            specific_where_clauses = common_where_clauses + ["e.comment_id IS NOT NULL"]
            if similarity is not None:
                specific_where_clauses.append(
                    "(1 - (e.embedding <=> CAST(:query_vector AS vector))) >= :similarity"
                )
            where_sql = " AND ".join(specific_where_clauses)
            sql = f"""
                WITH results AS (
                    SELECT
                        c.id, c.body, c.type, c.issue_id, c.author,
                        c.meta_data AS comment_meta_data,
                        c.created_at AS comment_created_at,
                        c.updated_at AS updated_at, -- Alias for sorting
                        (1 - (e.embedding <=> CAST(:query_vector AS vector))) as sim
                    FROM
                        comment c
                    JOIN
                        issueembedding e ON c.id = e.comment_id
                    JOIN
                        issue i ON c.issue_id = i.id
                    JOIN
                        tracker t ON i.tracker_id = t.id
                    WHERE {where_sql}
                )
                SELECT * FROM results
                ORDER BY {order_by_sql}
                LIMIT :limit OFFSET :skip
            """
            query_results = db.execute(text(sql), params).fetchall()
            for row in query_results:
                comment = Comment(
                    id=row.id,
                    body=row.body,
                    type=row.type,
                    issue_id=row.issue_id,
                    author=row.author,
                    meta_data=json.loads(row.comment_meta_data)
                    if row.comment_meta_data and isinstance(row.comment_meta_data, str)
                    else row.comment_meta_data,
                    created_at=row.comment_created_at,
                    updated_at=row.updated_at,
                )
                processed_results.append((comment, row.sim))

        elif embedding_type is None:
            # Build separate WHERE clauses for issues and comments
            where_sql_issues_part = " AND ".join(
                common_where_clauses + ["e.comment_id IS NULL"]
            )
            where_sql_comments_part = " AND ".join(
                common_where_clauses + ["e.comment_id IS NOT NULL"]
            )

            sql = f"""
                WITH combined_embeddings AS (
                    -- Issue Embeddings
                    SELECT
                        'issue' AS item_type,
                        e.embedding AS embedding_vector,
                        i.updated_at AS updated_at, -- Unified sort column
                        i.id AS issue_obj_id, i.title, i.description, i.status, i.priority, i.issue_type,
                        i.external_id, i.external_url, i.key, i.project_id AS issue_project_id, i.tracker_id AS issue_tracker_id,
                        i.meta_data AS issue_meta_data, i.last_updated_external, i.last_synced,
                        i.created_at AS issue_created_at, i.updated_at AS issue_updated_at,
                        NULL AS comment_obj_id, NULL AS comment_body, NULL AS comment_type, NULL AS comment_issue_id,
                        NULL AS comment_author, NULL AS comment_meta_data,
                        NULL AS comment_created_at, NULL AS comment_updated_at
                    FROM issueembedding e JOIN issue i ON e.issue_id = i.id
                    JOIN tracker t ON i.tracker_id = t.id
                    WHERE {where_sql_issues_part}

                    UNION ALL

                    -- Comment Embeddings
                    SELECT
                        'comment' AS item_type,
                        e.embedding AS embedding_vector,
                        c.updated_at AS updated_at, -- Unified sort column
                        NULL, NULL, NULL, NULL, NULL, NULL, -- Issue specific fields
                        NULL, NULL, NULL, i.project_id, i.tracker_id, -- Parent issue's project/tracker id
                        NULL, NULL, NULL, NULL, NULL, -- More issue fields
                        c.id AS comment_obj_id, c.body AS comment_body, c.type AS comment_type, c.issue_id AS comment_issue_id,
                        c.author AS comment_author, c.meta_data AS comment_meta_data,
                        c.created_at AS comment_created_at, c.updated_at AS comment_updated_at
                    FROM issueembedding e
                        JOIN comment c ON e.comment_id = c.id
                        JOIN issue i ON c.issue_id = i.id
                        JOIN tracker t ON i.tracker_id = t.id
                    WHERE {where_sql_comments_part}
                )
                SELECT
                    *,
                    (1 - (embedding_vector <=> CAST(:query_vector AS vector))) as sim
                FROM combined_embeddings
                ORDER BY {order_by_sql}
                LIMIT :limit OFFSET :skip
            """
            query_results = db.execute(text(sql), params).fetchall()
            for row in query_results:
                sim_score = row.sim
                if row.item_type == "issue":
                    issue = Issue(
                        id=row.issue_obj_id,
                        title=row.title,
                        description=row.description,
                        status=row.status,
                        priority=row.priority,
                        issue_type=row.issue_type,
                        external_id=row.external_id,
                        external_url=row.external_url,
                        key=row.key,
                        project_id=row.issue_project_id,
                        tracker_id=row.issue_tracker_id,
                        meta_data=json.loads(row.issue_meta_data)
                        if row.issue_meta_data and isinstance(row.issue_meta_data, str)
                        else row.issue_meta_data,
                        last_updated_external=row.last_updated_external,
                        last_synced=row.last_synced,
                        created_at=row.issue_created_at,
                        updated_at=row.updated_at,
                    )
                    processed_results.append((issue, sim_score))
                elif row.item_type == "comment":
                    comment = Comment(
                        id=row.comment_obj_id,
                        body=row.comment_body,
                        type=row.comment_type,
                        issue_id=row.comment_issue_id,
                        author=row.comment_author,
                        meta_data=json.loads(row.comment_meta_data)
                        if row.comment_meta_data
                        and isinstance(row.comment_meta_data, str)
                        else row.comment_meta_data,
                        created_at=row.comment_created_at,
                        updated_at=row.comment_updated_at,
                    )
                    processed_results.append((comment, sim_score))
        else:
            raise ValueError(
                f"Unsupported embedding_type: {embedding_type}. Must be 'issue', 'comment', or None."
            )

        return processed_results

    def get_embeddings_by_issue_ids(
        self, db: Session, *, issue_ids: List[str]
    ) -> Dict[str, List[IssueEmbedding]]:
        """Get all embeddings for a list of issue IDs, grouped by issue_id."""
        embeddings_query = (
            db.query(IssueEmbedding)
            .filter(IssueEmbedding.issue_id.in_(issue_ids))
            .all()
        )

        results: Dict[str, List[IssueEmbedding]] = {
            issue_id: [] for issue_id in issue_ids
        }
        for embedding in embeddings_query:
            results[embedding.issue_id].append(embedding)

        return results


# Initialize CRUDIssueEmbedding instance for easy import
crud_embedding_model = CRUDEmbeddingModel(EmbeddingModel)
crud_issue_embedding = CRUDIssueEmbedding(IssueEmbedding)
