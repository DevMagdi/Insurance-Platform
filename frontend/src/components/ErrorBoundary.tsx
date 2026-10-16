import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props { children: ReactNode }
interface State { hasError: boolean }
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };
  static getDerivedStateFromError(): State { return { hasError: true }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('UI boundary caught an error', error, info.componentStack); }
  render() {
    if (this.state.hasError) return <main className="grid min-h-screen place-items-center p-6 text-center"><div><h1 className="text-2xl font-bold">حدث خطأ غير متوقع</h1><p className="mt-3 text-slate-500">يرجى تحديث الصفحة والمحاولة مرة أخرى.</p><button className="mt-5 rounded-xl bg-slate-900 px-5 py-3 text-white" onClick={() => window.location.reload()}>إعادة تحميل الصفحة</button></div></main>;
    return this.props.children;
  }
}
