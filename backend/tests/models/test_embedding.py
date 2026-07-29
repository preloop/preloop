"""Tests for embedding functionality."""

from preloop.models.crud import crud_embedding_model, crud_issue_embedding, crud_comment
from preloop.models.models.issue import IssueEmbedding
from preloop.models.models.comment import Comment
from preloop.models.models import Issue  # Ensure Issue is imported

from sqlalchemy.orm import Session

import pytest


def test_create_embedding_model(db_session):
    """Test creating a new embedding model."""
    model_data = {
        "name": "test-embeddings",
        "provider": "openai",
        "version": "text-embedding-ada-002",
        "dimensions": 1536,
        "is_active": True,
        "meta_data": {},
    }

    model = crud_embedding_model.create(db_session, obj_in=model_data)
    assert model.name == model_data["name"]
    assert model.provider == model_data["provider"]
    assert model.dimensions == model_data["dimensions"]
    assert model.is_active is True

    # Verify it was saved in the database
    db_model = crud_embedding_model.get(db_session, id=model.id)
    assert db_model is not None
    assert db_model.id == model.id


def test_create_issue_embedding(
    db_session, create_issue, create_embedding_model, mocker
):
    """Test creating an issue embedding."""
    # Create test issue and embedding model
    issue = create_issue()
    model = create_embedding_model()

    # Mock the embedding vector generation
    mock_generate_embedding = mocker.patch(
        "preloop.models.crud.crud_issue_embedding._generate_embedding_vector"
    )
    mock_generate_embedding.return_value = [0.1] * model.dimensions

    # Create embedding for the issue
    result = crud_issue_embedding.create_embeddings(
        db_session, issue_id=issue.id, force_update=False
    )

    # Check result contains the model name with success status
    assert model.name in result
    assert "created" in result[model.name] or "exists" in result[model.name]

    # Verify embedding was created in the database
    embeddings = crud_issue_embedding.get_for_issue(db_session, issue_id=issue.id)
    assert model.name in embeddings

    # Verify the embedding has the correct dimension
    embedding = embeddings[model.name]
    assert len(embedding.embedding) == model.dimensions


def test_create_comment_embedding(
    db_session, create_issue, create_comment, create_embedding_model, mocker
):
    """Test creating an embedding for a specific comment."""
    issue = create_issue()
    comment = create_comment(
        issue_id=str(issue.id), external_id="901", body="This is a test comment."
    )
    model = create_embedding_model()

    # Mock the embedding vector generation
    mock_generate_embedding = mocker.patch(
        "preloop.models.crud.crud_issue_embedding._generate_embedding_vector"
    )
    mock_generate_embedding.return_value = [0.1] * model.dimensions

    # Create embedding for the comment
    result = crud_issue_embedding.create_embeddings(
        db_session, issue_id=issue.id, comment_id=comment.id, force_update=False
    )

    assert model.name in result
    assert (
        f"created_for_comment {comment.id}" in result[model.name]
        or "exists" in result[model.name]
    )

    # Verify embedding was created in the database for the comment
    comment_embeddings = crud_issue_embedding.get_for_comment(
        db_session, comment_id=comment.id
    )
    assert model.name in comment_embeddings
    embedding = comment_embeddings[model.name]
    assert embedding.comment_id == comment.id
    assert embedding.issue_id == issue.id
    assert len(embedding.embedding) == model.dimensions

    # Verify it's not mistaken for an issue content embedding
    issue_content_embeddings = crud_issue_embedding.get_for_issue_content(
        db_session, issue_id=issue.id
    )
    assert model.name not in issue_content_embeddings


def test_get_issue_content_embedding_distinct_from_comment(
    db_session, create_issue, create_comment, create_embedding_model, mocker
):
    """Test that issue content embeddings are distinct from comment embeddings."""
    issue = create_issue()
    comment = create_comment(
        issue_id=str(issue.id), external_id="901", body="Another test comment."
    )
    model = create_embedding_model()

    # Mock the embedding vector generation
    mock_generate_embedding = mocker.patch(
        "preloop.models.crud.crud_issue_embedding._generate_embedding_vector"
    )
    mock_generate_embedding.return_value = [0.1] * model.dimensions

    # Create embedding for issue content
    crud_issue_embedding.create_embeddings(db_session, issue_id=issue.id)
    # Create embedding for comment
    crud_issue_embedding.create_embeddings(
        db_session, issue_id=issue.id, comment_id=comment.id
    )

    # Get issue content embeddings
    issue_embeddings = crud_issue_embedding.get_for_issue_content(
        db_session, issue_id=issue.id
    )
    assert model.name in issue_embeddings
    assert issue_embeddings[model.name].comment_id is None

    # Get comment embeddings
    comment_embeddings = crud_issue_embedding.get_for_comment(
        db_session, comment_id=comment.id
    )
    assert model.name in comment_embeddings
    assert comment_embeddings[model.name].comment_id == comment.id


def test_delete_comment_cascades_embeddings(
    db_session, create_issue, create_comment, create_embedding_model, mocker
):
    """Test that deleting a comment also deletes its associated embeddings."""
    issue = create_issue()
    comment = create_comment(
        issue_id=str(issue.id), external_id="901", body="Comment to be deleted."
    )
    model = create_embedding_model()

    # Mock the private method that generates embeddings
    mock_generate_embedding = mocker.patch(
        "preloop.models.crud.crud_issue_embedding._generate_embedding_vector"
    )
    mock_generate_embedding.return_value = [0.1] * model.dimensions

    # Create embedding for the comment
    crud_issue_embedding.create_embeddings(
        db_session, issue_id=issue.id, comment_id=comment.id
    )

    comment_embeddings_before_delete = crud_issue_embedding.get_for_comment(
        db_session, comment_id=comment.id
    )
    assert model.name in comment_embeddings_before_delete
    embedding_id = comment_embeddings_before_delete[model.name].id

    # Delete the comment
    crud_comment.delete(db_session, id=comment.id)

    # Verify the comment is deleted
    deleted_comment = db_session.get(Comment, comment.id)
    assert deleted_comment is None

    # Verify the embedding associated with the comment is also deleted
    retrieved_embedding = db_session.get(IssueEmbedding, embedding_id)
    assert retrieved_embedding is None

    # Verify using the CRUD method too
    comment_embeddings_after_delete = crud_issue_embedding.get_for_comment(
        db_session, comment_id=comment.id
    )
    assert model.name not in comment_embeddings_after_delete


def test_similarity_search(
    db_session, create_issue, create_embedding_model, create_tracker
):
    """Test similarity search functionality."""
    # Create embedding model (defaulting to 1536 dimensions from conftest fixture)
    model = create_embedding_model()

    # Create a single tracker instance using the injected factory fixture
    # The `create_tracker` argument in the function signature provides the factory function.
    tracker = create_tracker()  # Call the factory to create the tracker

    # Create test issues using the *same* tracker instance
    issue1 = create_issue(title="Bug in login feature", tracker=tracker)
    issue2 = create_issue(title="Feature request: dashboard", tracker=tracker)
    issue3 = create_issue(title="Login page crashes on mobile", tracker=tracker)

    # Create fixed embeddings for testing similarity
    default_dimension = 1536  # Should match the default in EmbeddingModel or conftest
    embeddings_data = [
        ([1.0, 0.0, 0.0, 0.0], default_dimension),  # issue1
        ([0.0, 1.0, 0.0, 0.0], default_dimension),  # issue2
        ([0.8, 0.2, 0.0, 0.0], default_dimension),  # issue3 (more similar to issue1)
    ]
    embeddings = []
    for vec, dim in embeddings_data:
        embeddings.append(vec + [0.0] * (dim - len(vec)))

    # Manually create embeddings
    for issue, emb in zip([issue1, issue2, issue3], embeddings, strict=False):
        embedding = IssueEmbedding(
            issue_id=issue.id,
            embedding_model_id=model.id,
            embedding=emb,
            meta_data={"test": True},
        )
        db_session.add(embedding)
    db_session.commit()

    # Search with a vector similar to issue1
    query_vector_short = [0.9, 0.1, 0.0, 0.0]
    query_vector = query_vector_short + [0.0] * (
        default_dimension - len(query_vector_short)
    )
    # Get the tracker_id from one of the created issues
    tracker_id = tracker.id  # Use the id from the shared tracker created above
    results = crud_issue_embedding.similarity_search(
        db_session,
        model_id=model.id,
        query_vector=query_vector,
        distance_type="cosine",
        tracker_ids=[tracker_id],  # Pass the tracker_id
    )

    # Check we got results in the right order (issue1, issue3, issue2)
    assert len(results) == 3
    assert results[0][0].id == issue1.id  # Most similar
    assert results[1][0].id == issue3.id  # Second most similar
    assert results[2][0].id == issue2.id  # Least similar


def test_similarity_search_with_embedding_type(
    db_session: Session,
    create_issue,
    create_embedding_model,
    create_tracker,
    create_comment,
):
    """Test similarity search with embedding_type filtering."""
    # Use default 1536 dimensions for the model
    model = create_embedding_model(name="test_model_type_filter")
    default_dimension = (
        1536  # model.dimensions would also work if fixture guarantees it
    )
    tracker = create_tracker(name="test_tracker_type_filter")

    issue1 = create_issue(title="Issue One For Type Filter", tracker_id=tracker.id)
    issue2 = create_issue(title="Issue Two For Type Filter", tracker_id=tracker.id)

    # create_comment should return the Comment object and handle db_session internally or take it as arg
    comment1 = create_comment(
        issue_id=str(issue1.id), external_id="901", body="A comment on Issue One"
    )

    embedding_vector_issue1_short = [1.0, 0.0, 0.0]
    embedding_vector_issue2_short = [0.0, 1.0, 0.0]
    embedding_vector_comment1_short = [
        0.0,
        0.0,
        1.0,
    ]  # Distinct embedding for the comment

    embedding_vector_issue1 = embedding_vector_issue1_short + [0.0] * (
        default_dimension - len(embedding_vector_issue1_short)
    )
    embedding_vector_issue2 = embedding_vector_issue2_short + [0.0] * (
        default_dimension - len(embedding_vector_issue2_short)
    )
    embedding_vector_comment1 = embedding_vector_comment1_short + [0.0] * (
        default_dimension - len(embedding_vector_comment1_short)
    )

    # Create embeddings directly for precise testing
    embedding_for_issue1 = IssueEmbedding(
        issue_id=issue1.id,
        embedding_model_id=model.id,
        embedding=embedding_vector_issue1,
        comment_id=None,  # Explicitly for an issue embedding
    )
    embedding_for_issue2 = IssueEmbedding(
        issue_id=issue2.id,
        embedding_model_id=model.id,
        embedding=embedding_vector_issue2,
        comment_id=None,  # Explicitly for an issue embedding
    )
    embedding_for_comment1 = IssueEmbedding(
        issue_id=comment1.issue_id,  # Link to the parent issue of the comment
        comment_id=comment1.id,
        embedding_model_id=model.id,
        embedding=embedding_vector_comment1,
    )
    db_session.add_all(
        [embedding_for_issue1, embedding_for_issue2, embedding_for_comment1]
    )
    db_session.commit()

    # Test with embedding_type="issue"
    results_issue_type_query_issue1_vec = crud_issue_embedding.similarity_search(
        db_session,
        model_id=model.id,
        query_vector=embedding_vector_issue1,
        embedding_type="issue",
        limit=5,
    )
    assert len(results_issue_type_query_issue1_vec) == 2
    assert results_issue_type_query_issue1_vec[0][0].id == issue1.id
    assert isinstance(results_issue_type_query_issue1_vec[0][0], Issue)
    assert isinstance(results_issue_type_query_issue1_vec[1][0], Issue)

    results_issue_type_query_comment1_vec = crud_issue_embedding.similarity_search(
        db_session,
        model_id=model.id,
        query_vector=embedding_vector_comment1,
        embedding_type="issue",
        limit=5,
    )
    assert len(results_issue_type_query_comment1_vec) == 2
    assert isinstance(results_issue_type_query_comment1_vec[0][0], Issue)
    assert isinstance(results_issue_type_query_comment1_vec[1][0], Issue)

    # Test with embedding_type="comment"
    results_comment_type_query_comment1_vec = crud_issue_embedding.similarity_search(
        db_session,
        model_id=model.id,
        query_vector=embedding_vector_comment1,
        embedding_type="comment",
        limit=5,
    )
    assert len(results_comment_type_query_comment1_vec) == 1
    assert (
        results_comment_type_query_comment1_vec[0][0].id == comment1.id
    )  # Should return the Comment object itself
    assert isinstance(results_comment_type_query_comment1_vec[0][0], Comment)

    results_comment_type_query_issue1_vec = crud_issue_embedding.similarity_search(
        db_session,
        model_id=model.id,
        query_vector=embedding_vector_issue1,
        embedding_type="comment",
        limit=5,
    )
    assert len(results_comment_type_query_issue1_vec) == 1
    assert (
        results_comment_type_query_issue1_vec[0][0].id == comment1.id
    )  # The only comment available
    assert isinstance(results_comment_type_query_issue1_vec[0][0], Comment)

    # Test with embedding_type=None (mixed results), querying with issue1's vector
    results_none_type_query_issue1_vec = crud_issue_embedding.similarity_search(
        db_session,
        model_id=model.id,
        query_vector=embedding_vector_issue1,
        embedding_type=None,
        limit=5,
    )
    assert len(results_none_type_query_issue1_vec) == 3
    assert results_none_type_query_issue1_vec[0][0].id == issue1.id
    assert isinstance(results_none_type_query_issue1_vec[0][0], Issue)

    returned_ids_and_types_issue_query = {
        (item.id, type(item)) for item, score in results_none_type_query_issue1_vec
    }
    expected_ids_and_types_issue_query = {
        (issue1.id, Issue),
        (issue2.id, Issue),
        (comment1.id, Comment),
    }
    assert returned_ids_and_types_issue_query == expected_ids_and_types_issue_query

    # Test with embedding_type=None (mixed results), querying with comment1's vector
    results_none_type_query_comment1_vec = crud_issue_embedding.similarity_search(
        db_session,
        model_id=model.id,
        query_vector=embedding_vector_comment1,
        embedding_type=None,
        limit=5,
    )
    assert len(results_none_type_query_comment1_vec) == 3
    assert results_none_type_query_comment1_vec[0][0].id == comment1.id
    assert isinstance(results_none_type_query_comment1_vec[0][0], Comment)

    returned_ids_and_types_comment_query = {
        (item.id, type(item)) for item, score in results_none_type_query_comment1_vec
    }
    expected_ids_and_types_comment_query = {
        (comment1.id, Comment),
        (issue1.id, Issue),
        (issue2.id, Issue),
    }
    assert returned_ids_and_types_comment_query == expected_ids_and_types_comment_query

    # Test with an invalid embedding_type (should raise ValueError)
    with pytest.raises(
        ValueError, match="Unsupported embedding_type: invalid_type_string"
    ):
        crud_issue_embedding.similarity_search(
            db_session,
            model_id=model.id,
            query_vector=embedding_vector_issue1,
            embedding_type="invalid_type_string",
            limit=5,
        )

    with pytest.raises(ValueError, match="Unsupported embedding_type: another_invalid"):
        crud_issue_embedding.similarity_search(
            db_session,
            model_id=model.id,
            query_vector=embedding_vector_comment1,
            embedding_type="another_invalid",
            limit=5,
        )


def test_create_embeddings_concurrent_insert_race(
    db_session, create_issue, create_comment, create_embedding_model, mocker
):
    """A concurrent insert between the existence check and our insert must not
    blow up with IntegrityError (uix_issue_embedding_model) — it should fall
    back to updating the concurrently-inserted row (GlitchTip issue 2526).
    """
    from preloop.models.models.issue import EmbeddingModel

    issue = create_issue()
    comment = create_comment(
        issue_id=str(issue.id), external_id="902", body="Race condition comment."
    )
    model = create_embedding_model()

    # Deactivate any other (seeded) embedding models so the simulated
    # concurrent insert below races against exactly our model's row.
    db_session.query(EmbeddingModel).filter(EmbeddingModel.id != model.id).update(
        {"is_active": False}
    )
    db_session.flush()

    mock_generate_embedding = mocker.patch(
        "preloop.models.crud.crud_issue_embedding._generate_embedding_vector"
    )

    def _simulate_concurrent_insert(*args, **kwargs):
        # Another webhook delivery wins the race: insert the row AFTER the
        # existence check ran but BEFORE create_embeddings inserts its own.
        competing = IssueEmbedding(
            id=IssueEmbedding.generate_id(),
            issue_id=issue.id,
            comment_id=comment.id,
            embedding_model_id=model.id,
            embedding=[0.5] * model.dimensions,
            meta_data={"source": "concurrent writer"},
        )
        db_session.add(competing)
        db_session.flush()
        return [0.1] * model.dimensions

    mock_generate_embedding.side_effect = _simulate_concurrent_insert

    result = crud_issue_embedding.create_embeddings(
        db_session, issue_id=issue.id, comment_id=comment.id, force_update=False
    )

    # No "error_for_..." result: the duplicate insert was recovered by
    # updating the concurrently-created row.
    assert model.name in result
    assert "error" not in result[model.name]
    assert f"updated_for_comment {comment.id}" in result[model.name]

    embeddings = crud_issue_embedding.get_for_comment(db_session, comment_id=comment.id)
    embedding = embeddings[model.name]
    # The recovered row now carries our vector, not the competing one.
    assert embedding.embedding[0] == pytest.approx(0.1)
