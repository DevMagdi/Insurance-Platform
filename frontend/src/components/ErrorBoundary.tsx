import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /**
   * Rendered instead of the full-page fallback. A section-level boundary (for
   * example the operations table) uses this so one bad row degrades to a
   * message inside the page instead of replacing the whole console.
   */
  fallback?: ReactNode;
}
interface State { hasError: boolean }
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };
  static getDerivedStateFromError(): State { return { hasError: true }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('UI boundary caught an error', error, info.componentStack); }
  render() {
    if (this.state.hasError) {
      if (this.props.fallback !== undefined) return this.props.fallback;
      return <main className="grid min-h-screen place-items-center p-6 text-center"><div><h1 className="text-2xl font-bold">حدث خطأ غير متوقع</h1><p className="mt-3 text-text-muted">يرجى تحديث الصفحة والمحاولة مرة أخرى.</p><button className="btn btn-primary btn-lg mt-5" onClick={() => window.location.reload()}>إعادة تحميل الصفحة</button></div></main>;
    }
    return this.props.children;
  }
}
