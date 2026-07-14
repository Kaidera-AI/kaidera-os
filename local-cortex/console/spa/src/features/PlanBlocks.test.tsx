import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DataModelBlock, ApiEndpointBlock, AnnotatedCodeBlock, FileTreeBlock } from './PlanBlocks'

describe('PlanBlocks', () => {
  // data-model is now a multi-entity ERD (forked lean from agent-native).
  const erd = `
entities:
  - name: Project
    fields:
      - { name: id, type: uuid, pk: true }
      - { name: key, type: text }
  - name: Worker
    fields:
      - { name: id, type: uuid, pk: true }
      - { name: project_id, type: uuid, fk: Project.id }
`

  it('renders every entity + an FK chip resolving the target entity.field', () => {
    render(<DataModelBlock body={erd} />)
    // Names appear in the entity header AND the inferred-relation row → use getAllByText.
    expect(screen.getAllByText('Project').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Worker').length).toBeGreaterThan(0)
    expect(screen.getByText('Project.id')).toBeInTheDocument() // parseFk + resolveEntity
  })

  it('infers a relation from an fk field when relations are omitted', () => {
    render(<DataModelBlock body={erd} />)
    expect(screen.getByText('Relations')).toBeInTheDocument() // effectiveRelations inference
  })

  it('renders a diff chip for a changed field', () => {
    render(<DataModelBlock body={'entities:\n  - name: Project\n    fields:\n      - { name: slug, type: text, change: added }'} />)
    expect(screen.getByText('Added')).toBeInTheDocument()
  })

  it('collapses all but the first entity when the model is large (>2)', () => {
    const three = `entities:
  - { name: A, fields: [{ name: a1, type: int }] }
  - { name: B, fields: [{ name: b1, type: int }] }
  - { name: C, fields: [{ name: c1secret, type: int }] }`
    render(<DataModelBlock body={three} />)
    expect(screen.queryByText('c1secret')).not.toBeInTheDocument() // collapsed
    fireEvent.click(screen.getByText('C'))
    expect(screen.getByText('c1secret')).toBeInTheDocument() // expands on click
  })

  it('renders an api-endpoint with its method + path', () => {
    render(<ApiEndpointBlock body={'method: post\npath: /agents/{p}/{a}/chat'} />)
    expect(screen.getByText('POST')).toBeInTheDocument() // upper-cased
    expect(screen.getByText('/agents/{p}/{a}/chat')).toBeInTheDocument()
  })

  it('renders annotated-code with line numbers + a note label', () => {
    render(<AnnotatedCodeBlock body={'code: |\n  a = 1\n  b = 2\nnotes:\n  - {lines: "2", label: second, note: the b line}'} />)
    expect(screen.getByText('a = 1')).toBeInTheDocument()
    expect(screen.getByText(/second/)).toBeInTheDocument()
    expect(screen.getByText('L2')).toBeInTheDocument()
  })

  it('renders a file-tree item with its note', () => {
    render(<FileTreeBlock body={'root: app\nitems:\n  - {path: main.py, note: the shell}'} />)
    expect(screen.getByText('main.py')).toBeInTheDocument()
    expect(screen.getByText('the shell')).toBeInTheDocument()
  })

  it('fails soft on bad YAML (amber box, no throw)', () => {
    render(<DataModelBlock body={'entities: : :['} />)
    expect(screen.getByText(/data-model parse error/)).toBeInTheDocument()
  })
})
