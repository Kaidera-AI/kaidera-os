import type { ReactElement } from 'react'
import { AnnotatedCodeBlock, ApiEndpointBlock, DataModelBlock, FileTreeBlock } from './PlanBlocks'

/** Block tag → component, for the MdxPlanRenderer code-fence interceptor. */
export const PLAN_BLOCKS: Record<string, (p: { body: string }) => ReactElement> = {
  'data-model': DataModelBlock,
  'api-endpoint': ApiEndpointBlock,
  'annotated-code': AnnotatedCodeBlock,
  'file-tree': FileTreeBlock,
}
