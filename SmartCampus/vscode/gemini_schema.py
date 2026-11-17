"""Translate our JSON schemas to Gemini's native structured-output schema.

The complete original JSON schema still validates each response locally.
"""
def native_schema(schema):
    supported = {'type','description','enum','properties','required','items',
                 'nullable','minItems','maxItems','minimum','maximum','format'}
    definitions = schema.get('$defs', {})
    def convert(node):
        if isinstance(node,list):return [convert(x) for x in node]
        if not isinstance(node,dict):return node
        if '$ref' in node:return convert(definitions[node['$ref'].removeprefix('#/$defs/')])
        result={key:({name:convert(child) for name,child in value.items()} if key=='properties' else convert(value))
                for key,value in node.items() if key in supported}
        # Native Schema.enum is a string list. Preserve numeric/boolean enum
        # validation in the ORIGINAL local JSON schema, never stringify scores.
        if 'enum' in result and any(not isinstance(x,str) for x in result['enum']):
            result.pop('enum')
        return result
    return convert(schema)
