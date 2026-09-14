import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../lib/utils'

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors disabled:opacity-50 cursor-pointer whitespace-nowrap',
  { variants: {
      variant: { default:'bg-accent text-accent-fg hover:opacity-90',
                 outline:'border bg-panel text-ink hover:bg-canvas',
                 ghost:'text-mute hover:bg-canvas hover:text-ink' },
      size: { sm:'h-8 px-3', md:'h-9 px-4', icon:'h-9 w-9' } },
    defaultVariants: { variant:'default', size:'md' } })

export function Button({ className, variant, size, ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...p} />
}

export function Card({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) { return <div className={cn('rounded-lg border bg-panel', className)} {...p} /> }
export function CardHeader({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) { return <div className={cn('flex items-center gap-2 px-5 py-4 border-b', className)} {...p} /> }
export function CardTitle({ className, ...p }: React.HTMLAttributes<HTMLHeadingElement>) { return <h3 className={cn('text-sm font-semibold', className)} {...p} /> }
export function CardContent({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) { return <div className={cn('p-5', className)} {...p} /> }

const badgeVariants = cva('inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold',
  { variants: { variant: {
      ok:'bg-ok-bg text-ok', danger:'bg-danger-bg text-danger', warn:'bg-warn-bg text-warn',
      idle:'bg-idle-bg text-idle', outline:'border text-mute' } },
    defaultVariants: { variant:'outline' } })
export function Badge({ className, variant, dot=true, children, ...p }: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants> & { dot?: boolean }) {
  return <span className={cn(badgeVariants({ variant }), className)} {...p}>{dot && <span className="h-1.5 w-1.5 rounded-full bg-current opacity-90" />}{children}</span>
}

export function Table({ className, ...p }: React.TableHTMLAttributes<HTMLTableElement>) { return <table className={cn('w-full text-sm', className)} {...p} /> }
export function Th({ className, ...p }: React.ThHTMLAttributes<HTMLTableCellElement>) { return <th className={cn('text-left font-medium text-mute text-[11px] uppercase tracking-wider px-5 py-2.5 border-b', className)} {...p} /> }
export function Td({ className, ...p }: React.TdHTMLAttributes<HTMLTableCellElement>) { return <td className={cn('px-5 py-3 align-middle', className)} {...p} /> }
export function Tr({ className, ...p }: React.HTMLAttributes<HTMLTableRowElement>) { return <tr className={cn('border-b last:border-0', className)} {...p} /> }
