import { lazy, Suspense } from 'react';


const MarkdownRenderer = lazy(() => import('./MarkdownRenderer'));


export default function LazyMarkdownRenderer({ content }: { content: string }) {
  return (
    <Suspense fallback={<div className="markdown-body">{content}</div>}>
      <MarkdownRenderer content={content} />
    </Suspense>
  );
}
