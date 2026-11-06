"""A minimal indexing pipeline for the deploy-index tests: convert text files, split, write."""

from haystack import Pipeline
from haystack.components.converters import TextFileToDocument
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter
from haystack.document_stores.in_memory import InMemoryDocumentStore

indexing = Pipeline()
indexing.add_component("converter", TextFileToDocument())
indexing.add_component("splitter", DocumentSplitter())
indexing.add_component("writer", DocumentWriter(document_store=InMemoryDocumentStore()))
indexing.connect("converter.documents", "splitter.documents")
indexing.connect("splitter.documents", "writer.documents")
