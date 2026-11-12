"""T-073: the OpenAPI / GraphQL parser (CAP-040) — pure functions, no database."""

import json
import unittest

from beework import api_spec
from beework.api_spec import SpecError, parse_graphql, parse_openapi, parse_spec

PETSTORE = """
openapi: 3.0.3
info: {title: Petstore, version: "1.0"}
servers: [{url: https://petstore.example.com/v1}]
paths:
  /pets:
    get:
      operationId: listPets
      summary: List all pets
      parameters:
        - {name: limit, in: query, required: false, schema: {type: integer, maximum: 100}}
    post:
      operationId: createPet
      summary: Create a pet
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: '#/components/schemas/NewPet'}
  /pets/{petId}:
    parameters:
      - {name: petId, in: path, required: true, schema: {type: string}}
    get:
      operationId: showPetById
      summary: Info for a specific pet
      parameters:
        - {name: X-Trace, in: header, schema: {type: string}}
    delete:
      summary: Remove a pet
components:
  schemas:
    NewPet:
      type: object
      required: [name]
      properties:
        name: {type: string}
        tag: {type: string}
        owner: {$ref: '#/components/schemas/Owner'}
    Owner:
      type: object
      properties:
        name: {type: string}
        pet: {$ref: '#/components/schemas/NewPet'}
"""

SDL = '''
"""The API"""
scalar DateTime
enum Status { OPEN CLOSED }
input NewTicket { title: String!, priority: Int = 3, status: Status }
type Ticket { id: ID!, title: String!, status: Status, created: DateTime, owner: User }
type User { id: ID!, name: String }
type Query {
  "Find one ticket"
  ticket(id: ID!): Ticket
  tickets(status: Status, first: Int = 10): [Ticket!]!
}
type Mutation {
  createTicket(input: NewTicket!): Ticket
  closeTicket(id: ID!): Boolean
}
'''


class OpenApiTests(unittest.TestCase):
    def tools(self, text: str = PETSTORE) -> dict[str, api_spec.ApiTool]:
        return {t.name: t for t in parse_openapi(text)}

    def test_petstore_yields_one_tool_per_path_and_method(self) -> None:
        tools = self.tools()
        self.assertEqual(sorted(tools), ["createPet", "delete_pets_petId", "listPets", "showPetById"])
        self.assertEqual([tools[n].writes for n in ("listPets", "showPetById", "createPet", "delete_pets_petId")], [False, False, True, True])

    def test_parameters_become_a_json_schema_and_a_binding(self) -> None:
        show = self.tools()["showPetById"]
        self.assertEqual(show.parameters["required"], ["petId"])  # a path parameter is always required (the path-level one is inherited)
        self.assertEqual(set(show.parameters["properties"]), {"petId", "X-Trace"})
        self.assertEqual(show.binding["method"], "GET")
        self.assertEqual((show.binding["path"], show.binding["path_params"], show.binding["header_params"]), ("/pets/{petId}", [{"name": "petId", "arg": "petId"}], [{"name": "X-Trace", "arg": "X-Trace"}]))
        listing = self.tools()["listPets"]
        self.assertEqual(listing.parameters["properties"]["limit"], {"type": "integer", "maximum": 100})
        self.assertEqual(listing.parameters["required"], [])

    def test_the_request_body_is_one_object_argument_with_refs_inlined(self) -> None:
        create = self.tools()["createPet"]
        self.assertEqual(create.parameters["required"], ["body"])
        body = create.parameters["properties"]["body"]
        self.assertEqual((body["type"], body["required"]), ("object", ["name"]))
        self.assertEqual(body["properties"]["owner"]["properties"]["name"], {"type": "string"})
        self.assertTrue(create.binding["body"])

    def test_a_reference_cycle_is_cut_not_followed_forever(self) -> None:
        body = self.tools()["createPet"].parameters["properties"]["body"]
        # NewPet -> Owner -> NewPet is cut when it comes back to itself
        self.assertIn("循环", json.dumps(body, ensure_ascii=False))

    def test_a_missing_operation_id_is_made_from_the_method_and_path(self) -> None:
        self.assertIn("delete_pets_petId", self.tools())
        spec = 'openapi: "3.1.0"\npaths:\n  /a/b-c:\n    get: {summary: x}\n    post: {summary: y}\n'
        self.assertEqual(sorted(self.tools(spec)), ["get_a_b-c", "post_a_b-c"])

    def test_duplicate_operation_ids_are_made_unique(self) -> None:
        spec = 'openapi: 3.0.0\npaths:\n  /a: {get: {operationId: dup}}\n  /b: {get: {operationId: dup}}\n'
        self.assertEqual(sorted(self.tools(spec)), ["dup", "dup_2"])

    def test_a_json_document_works_too(self) -> None:
        spec = json.dumps({"openapi": "3.0.0", "paths": {"/x": {"get": {"operationId": "x"}}}})
        self.assertEqual(list(self.tools(spec)), ["x"])

    def test_an_external_reference_is_refused_with_its_line(self) -> None:
        for ref in ("https://evil.example.com/schema.json#/Pet", "./other.yaml#/Pet", "file:///etc/passwd"):
            spec = f"openapi: 3.0.0\npaths:\n  /a:\n    post:\n      requestBody:\n        content:\n          application/json:\n            schema: {{$ref: '{ref}'}}\n"
            with self.assertRaises(SpecError) as caught:
                parse_openapi(spec)
            self.assertIn("外部", caught.exception.message)
            self.assertEqual(caught.exception.line, 8, ref)

    def test_a_missing_internal_reference_is_an_error_with_its_line(self) -> None:
        spec = "openapi: 3.0.0\npaths:\n  /a:\n    get:\n      parameters:\n        - $ref: '#/components/parameters/Nope'\n"
        with self.assertRaises(SpecError) as caught:
            parse_openapi(spec)
        self.assertEqual((caught.exception.line, "找不到引用" in caught.exception.message), (6, True))

    def test_syntax_errors_report_the_line(self) -> None:
        with self.assertRaises(SpecError) as caught:
            parse_openapi("openapi: 3.0.0\npaths:\n  /a:\n    get: [unclosed\n")
        self.assertIsNotNone(caught.exception.line)
        with self.assertRaises(SpecError) as caught:
            parse_openapi('{\n  "openapi": "3.0.0",\n  "paths": {,}\n}')
        self.assertEqual(caught.exception.line, 3)

    def test_other_documents_are_refused(self) -> None:
        for text, fragment in (("swagger: '2.0'\npaths: {}", "3.0"), ("openapi: 3.0.0", "paths"), ("[1, 2]", "对象"), ("openapi: 3.0.0\npaths:\n  /a: {}\n", "没有可用的接口"), ("", "为空")):
            with self.assertRaises(SpecError) as caught:
                parse_spec(text, "rest")
            self.assertIn(fragment, caught.exception.message, text)

    def test_a_very_large_document_is_refused(self) -> None:
        with self.assertRaises(SpecError):
            parse_openapi("openapi: 3.0.0\n# " + "x" * (api_spec.MAX_SPEC_BYTES + 1))

    def test_the_kind_is_guessed(self) -> None:
        self.assertEqual((api_spec.kind_of(PETSTORE), api_spec.kind_of('{"openapi":"3.0.0"}'), api_spec.kind_of(SDL)), ("openapi", "openapi", "graphql"))


class GraphqlTests(unittest.TestCase):
    def tools(self, text: str = SDL) -> dict[str, api_spec.ApiTool]:
        return {t.name: t for t in parse_graphql(text)}

    def test_every_query_and_mutation_field_is_a_tool_and_mutations_write(self) -> None:
        tools = self.tools()
        self.assertEqual(sorted(tools), ["closeTicket", "createTicket", "ticket", "tickets"])
        self.assertEqual({n: t.writes for n, t in tools.items()}, {"ticket": False, "tickets": False, "createTicket": True, "closeTicket": True})
        self.assertEqual(tools["ticket"].description, "Find one ticket")

    def test_arguments_become_a_json_schema(self) -> None:
        tools = self.tools()
        self.assertEqual(tools["ticket"].parameters, {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
        listing = tools["tickets"].parameters
        self.assertEqual((listing["properties"]["status"], listing["properties"]["first"], listing["required"]), ({"type": "string", "enum": ["OPEN", "CLOSED"]}, {"type": "integer"}, []))
        created = tools["createTicket"].parameters["properties"]["input"]
        self.assertEqual((created["type"], created["required"], created["properties"]["priority"]), ("object", ["title"], {"type": "integer"}))

    def test_the_binding_declares_the_variable_types_and_selects_scalar_fields(self) -> None:
        binding = self.tools()["tickets"].binding
        self.assertEqual((binding["operation"], binding["field"], binding["args"]), ("query", "tickets", {"status": "Status", "first": "Int"}))
        self.assertEqual(binding["selection"], " { id title status created }")  # scalars and enums, not the nested object
        self.assertEqual(self.tools()["closeTicket"].binding["selection"], "")  # a scalar return needs none

    def test_a_schema_block_can_rename_the_roots(self) -> None:
        sdl = "schema { query: RootQuery }\ntype RootQuery { ping: String }\n"
        self.assertEqual(list(self.tools(sdl)), ["ping"])

    def test_extend_type_and_comments_and_directives(self) -> None:
        sdl = "# the API\ntype Query { a: String @deprecated(reason: \"x\") }\nextend type Query { b(x: Int! = 5): Int }\n"
        self.assertEqual(sorted(self.tools(sdl)), ["a", "b"])

    def test_errors_report_the_line(self) -> None:
        with self.assertRaises(SpecError) as caught:
            parse_graphql("type Query {\n  a: String\n  b(\n")
        self.assertIsNotNone(caught.exception.line)
        with self.assertRaises(SpecError) as caught:
            parse_graphql("type Query { a: String }\n\n ?? nonsense")
        self.assertEqual(caught.exception.line, 3)
        with self.assertRaises(SpecError):
            parse_graphql("type Other { a: String }")  # no Query / Mutation

    def test_parse_spec_dispatches_on_the_type(self) -> None:
        self.assertEqual(len(parse_spec(SDL, "graphql")), 4)
        self.assertEqual(len(parse_spec(PETSTORE, "rest")), 4)


if __name__ == "__main__":
    unittest.main()
